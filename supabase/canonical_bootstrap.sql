-- ============================================================================
-- LifeOS / Telegram Self-Bot — Canonical Supabase Bootstrap & Reconciliation
--
-- Generated from a repository-wide database contract audit of commit
-- 30bb3a426c2ec419be9d8f43373d85ce27d77099 (origin/main). See
-- DATABASE_ARCHITECTURE.md §30 for the schema-drift audit this revision
-- resolves.
--
-- Properties
--   * Establishes the COMPLETE database state required by CURRENT code:
--     16 canonical tables (the 14 previously documented public-schema tables
--     plus the live task-system tables ai_tasks / ai_task_occurrences that the
--     20260829…–20260912… migrations create) + 1 compatibility-preserved
--     legacy table (ghost_chats, kept additively).
--   * EXISTING-DATABASE RECONCILIATION. `CREATE TABLE IF NOT EXISTS` is a
--     silent NO-OP when the table already exists, so it is NEVER the only
--     mechanism that establishes a column. Immediately after every CREATE,
--     every canonical column is re-asserted with
--     `ADD COLUMN IF NOT EXISTS`, existing rows are backfilled
--     deterministically, and the column is bound to its final default /
--     NOT NULL contract. This is the fix for the 42703 class of failure
--     (`column "..." does not exist`) that a legacy table shape used to cause
--     on `bot_settings.value_type`, on the `panel_settings` constraint blocks
--     and on the `ai_config` upsert columns.
--   * Safe on a FRESH database, safe on any older/partially-migrated one, and
--     safe to run twice (every statement is idempotent or catalog-guarded).
--   * Additive only: no DROP TABLE, no DROP COLUMN, no DELETE, no TRUNCATE and
--     no data destruction. The only intentional drops are stale anon WRITE
--     policies that contradict the documented SELECT-only dashboard boundary.
--     Constraint/index additions that existing data could reject are
--     data-guarded: they are skipped with an explicit WARNING naming the
--     offending row count instead of aborting the script.
--   * Security model: ALL writes use the service-role key (bypasses RLS);
--     anon + authenticated get SELECT-only (read-only dashboard). No anon
--     INSERT/UPDATE/DELETE policy exists on any canonical table.
--   * The optional Supabase Vault credential RPCs are NOT part of this script;
--     they are documented and applied separately in DATABASE_ARCHITECTURE.md
--     §29 (api_credentials, api_credential_pool, the management functions).
--   * Historical application data (saved items, AI sessions/messages/
--     memories/usage/stats, logs, allow-list values) is intentionally NOT
--     fabricated. Only deterministic, project-defined seed rows are inserted.
--   * The script ends with a drift report: after COMMIT it lists any canonical
--     (table, column) that is still absent. Zero rows == the database now
--     matches the canonical contract.
--
-- Execution: paste the whole file into the Supabase SQL Editor as `postgres`.
--
-- This text exists in exactly three places and they are kept byte-identical
-- (tests/test_canonical_schema_reconciliation.py enforces it):
--   1. supabase/migrations/20260920000001_reconcile_canonical_schema.sql
--      (the repository migration for this repair)
--   2. supabase/canonical_bootstrap.sql (this convenience copy)
--   3. the fenced SQL block in DATABASE_ARCHITECTURE.md §30
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
    display_name    text,
    short_code      text,
    owner_id        bigint       NOT NULL,
    created_at      timestamptz  DEFAULT now()
);

-- Existing-database column reconciliation (see the header).
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS id              bigserial;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS save_code       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS save_type       text        NOT NULL DEFAULT 'forward';
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS origin_chat_id  bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS origin_msg_id   bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS saved_chat_id   bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS saved_msg_id    bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS sender_name     text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS sender_id       bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS mime_type       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_id         text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_size       bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS media_type      text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS tags            text[]      DEFAULT '{}';
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS caption         text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_name       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS display_name    text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS short_code      text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS owner_id        bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();

-- Deterministic backfill so the NOT NULL contract below can never fail.
UPDATE saved_items SET save_code  = 'S' || lpad(id::text, 4, '0') WHERE save_code  IS NULL;
UPDATE saved_items SET save_type  = 'forward'  WHERE save_type  IS NULL;
UPDATE saved_items SET tags       = '{}'       WHERE tags       IS NULL;
UPDATE saved_items SET created_at = now()      WHERE created_at IS NULL;
UPDATE saved_items SET owner_id   = 0          WHERE owner_id   IS NULL;

ALTER TABLE saved_items ALTER COLUMN save_type  SET DEFAULT 'forward';
ALTER TABLE saved_items ALTER COLUMN tags       SET DEFAULT '{}';
ALTER TABLE saved_items ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE saved_items ALTER COLUMN save_code SET NOT NULL;
ALTER TABLE saved_items ALTER COLUMN save_type SET NOT NULL;
ALTER TABLE saved_items ALTER COLUMN owner_id  SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_saved_items_owner          ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code      ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at     ON saved_items (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created  ON saved_items (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm   ON saved_items USING gin (caption gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm ON saved_items USING gin (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm ON saved_items USING gin (save_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm ON saved_items USING gin (short_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm      ON saved_items USING gin (mime_type gin_trgm_ops);
-- Save V2 resolver indexes: display_name ILIKE (trigram) and whole-tag
-- array containment (tags.cs.{...}).
CREATE INDEX IF NOT EXISTS idx_saved_items_display_name_trgm ON saved_items USING gin (display_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_tags           ON saved_items USING gin (tags);

-- Data-guarded partial unique index: legacy rows may share a short_code.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'idx_saved_items_short_code') THEN
        IF NOT EXISTS (
            SELECT 1 FROM saved_items
            WHERE short_code IS NOT NULL
            GROUP BY short_code HAVING count(*) > 1
        ) THEN
            CREATE UNIQUE INDEX idx_saved_items_short_code
                ON saved_items (short_code) WHERE short_code IS NOT NULL;
        ELSE
            RAISE WARNING 'saved_items: idx_saved_items_short_code NOT created - duplicate non-NULL short_code values exist. Resolve them and re-run this script.';
        END IF;
    END IF;
END $$;

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

ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS template    text        NOT NULL DEFAULT '🕒 {time} | 💭 {mood}';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS mood        text        NOT NULL DEFAULT '😊';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS custom_text text        NOT NULL DEFAULT '';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS is_active   boolean     NOT NULL DEFAULT false;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS last_bio    text        NOT NULL DEFAULT '';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();

UPDATE bio_state SET owner_id    = 0                       WHERE owner_id    IS NULL;
UPDATE bio_state SET template    = '🕒 {time} | 💭 {mood}' WHERE template    IS NULL;
UPDATE bio_state SET mood        = '😊'                    WHERE mood        IS NULL;
UPDATE bio_state SET custom_text = ''                      WHERE custom_text IS NULL;
UPDATE bio_state SET is_active   = false                   WHERE is_active   IS NULL;
UPDATE bio_state SET last_bio    = ''                      WHERE last_bio    IS NULL;
UPDATE bio_state SET updated_at  = now()                   WHERE updated_at  IS NULL;

ALTER TABLE bio_state ALTER COLUMN template    SET DEFAULT '🕒 {time} | 💭 {mood}';
ALTER TABLE bio_state ALTER COLUMN mood        SET DEFAULT '😊';
ALTER TABLE bio_state ALTER COLUMN custom_text SET DEFAULT '';
ALTER TABLE bio_state ALTER COLUMN is_active   SET DEFAULT false;
ALTER TABLE bio_state ALTER COLUMN last_bio    SET DEFAULT '';
ALTER TABLE bio_state ALTER COLUMN updated_at  SET DEFAULT now();

ALTER TABLE bio_state ALTER COLUMN owner_id    SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN template    SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN mood        SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN custom_text SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN is_active   SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN last_bio    SET NOT NULL;

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

ALTER TABLE username_state ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS template    text        NOT NULL DEFAULT '{time} | {mood}';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS mood        text        NOT NULL DEFAULT '😊';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS custom_text text        NOT NULL DEFAULT '';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS is_active   boolean     NOT NULL DEFAULT false;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS last_name   text        NOT NULL DEFAULT '';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();

UPDATE username_state SET owner_id    = 0                 WHERE owner_id    IS NULL;
UPDATE username_state SET template    = '{time} | {mood}' WHERE template    IS NULL;
UPDATE username_state SET mood        = '😊'              WHERE mood        IS NULL;
UPDATE username_state SET custom_text = ''                WHERE custom_text IS NULL;
UPDATE username_state SET is_active   = false             WHERE is_active   IS NULL;
UPDATE username_state SET last_name   = ''                WHERE last_name   IS NULL;
UPDATE username_state SET updated_at  = now()             WHERE updated_at  IS NULL;

ALTER TABLE username_state ALTER COLUMN template    SET DEFAULT '{time} | {mood}';
ALTER TABLE username_state ALTER COLUMN mood        SET DEFAULT '😊';
ALTER TABLE username_state ALTER COLUMN custom_text SET DEFAULT '';
ALTER TABLE username_state ALTER COLUMN is_active   SET DEFAULT false;
ALTER TABLE username_state ALTER COLUMN last_name   SET DEFAULT '';
ALTER TABLE username_state ALTER COLUMN updated_at  SET DEFAULT now();

ALTER TABLE username_state ALTER COLUMN owner_id    SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN template    SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN mood        SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN custom_text SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN is_active   SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN last_name   SET NOT NULL;

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

ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS id         bigserial;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS owner_id   bigint      NOT NULL DEFAULT 0;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS level      text        NOT NULL DEFAULT 'INFO';
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS message    text;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS context    jsonb       DEFAULT '{}';
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

UPDATE bot_logs SET owner_id   = 0      WHERE owner_id   IS NULL;
UPDATE bot_logs SET level      = 'INFO' WHERE level      IS NULL;
UPDATE bot_logs SET message    = ''     WHERE message    IS NULL;
UPDATE bot_logs SET context    = '{}'   WHERE context    IS NULL;
UPDATE bot_logs SET created_at = now()  WHERE created_at IS NULL;

ALTER TABLE bot_logs ALTER COLUMN level      SET DEFAULT 'INFO';
ALTER TABLE bot_logs ALTER COLUMN context    SET DEFAULT '{}';
ALTER TABLE bot_logs ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE bot_logs ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE bot_logs ALTER COLUMN level    SET NOT NULL;
ALTER TABLE bot_logs ALTER COLUMN message  SET NOT NULL;

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
--
-- This is the SECOND instance of the drift class: 20260726143924 created only
-- (key, auto_close_enabled, updated_at), so the CHECK blocks further down
-- referenced columns a silent no-op CREATE never added.
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

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS key                          text;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_close_enabled           boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_close_delay             integer     NOT NULL DEFAULT 120;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS max_deep_save_mb             integer     NOT NULL DEFAULT 50;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS delete_batch_size            integer     NOT NULL DEFAULT 100;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS log_retention_days           integer     NOT NULL DEFAULT 7;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS panel_timeout_seconds        integer     NOT NULL DEFAULT 300;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS allow_multiple_panels        boolean     NOT NULL DEFAULT false;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS reuse_existing_panel         boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS language                     text        NOT NULL DEFAULT 'en';
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS debug_callbacks              boolean     NOT NULL DEFAULT false;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS owner_only                   boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS dashboard_font               text        NOT NULL DEFAULT 'default';
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS update_stale_seconds         integer     NOT NULL DEFAULT 300;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS ghost_seen_retention_seconds bigint      NOT NULL DEFAULT 2592000;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS updated_at                   timestamptz DEFAULT now();

UPDATE panel_settings SET auto_close_enabled           = true      WHERE auto_close_enabled           IS NULL;
UPDATE panel_settings SET auto_close_delay             = 120       WHERE auto_close_delay             IS NULL;
UPDATE panel_settings SET max_deep_save_mb             = 50        WHERE max_deep_save_mb             IS NULL;
UPDATE panel_settings SET delete_batch_size            = 100       WHERE delete_batch_size            IS NULL;
UPDATE panel_settings SET log_retention_days           = 7         WHERE log_retention_days           IS NULL;
UPDATE panel_settings SET panel_timeout_seconds        = 300       WHERE panel_timeout_seconds        IS NULL;
UPDATE panel_settings SET allow_multiple_panels        = false     WHERE allow_multiple_panels        IS NULL;
UPDATE panel_settings SET reuse_existing_panel         = true      WHERE reuse_existing_panel         IS NULL;
UPDATE panel_settings SET language                     = 'en'      WHERE language                     IS NULL;
UPDATE panel_settings SET debug_callbacks              = false     WHERE debug_callbacks              IS NULL;
UPDATE panel_settings SET owner_only                   = true      WHERE owner_only                   IS NULL;
UPDATE panel_settings SET dashboard_font               = 'default' WHERE dashboard_font               IS NULL;
UPDATE panel_settings SET update_stale_seconds         = 300       WHERE update_stale_seconds         IS NULL;
UPDATE panel_settings SET ghost_seen_retention_seconds = 2592000   WHERE ghost_seen_retention_seconds IS NULL;
UPDATE panel_settings SET updated_at                   = now()     WHERE updated_at                   IS NULL;

ALTER TABLE panel_settings ALTER COLUMN auto_close_enabled           SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN auto_close_delay             SET DEFAULT 120;
ALTER TABLE panel_settings ALTER COLUMN max_deep_save_mb             SET DEFAULT 50;
ALTER TABLE panel_settings ALTER COLUMN delete_batch_size            SET DEFAULT 100;
ALTER TABLE panel_settings ALTER COLUMN log_retention_days           SET DEFAULT 7;
ALTER TABLE panel_settings ALTER COLUMN panel_timeout_seconds        SET DEFAULT 300;
ALTER TABLE panel_settings ALTER COLUMN allow_multiple_panels        SET DEFAULT false;
ALTER TABLE panel_settings ALTER COLUMN reuse_existing_panel         SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN language                     SET DEFAULT 'en';
ALTER TABLE panel_settings ALTER COLUMN debug_callbacks              SET DEFAULT false;
ALTER TABLE panel_settings ALTER COLUMN owner_only                   SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN dashboard_font               SET DEFAULT 'default';
ALTER TABLE panel_settings ALTER COLUMN update_stale_seconds         SET DEFAULT 300;
ALTER TABLE panel_settings ALTER COLUMN ghost_seen_retention_seconds SET DEFAULT 2592000;
ALTER TABLE panel_settings ALTER COLUMN updated_at                   SET DEFAULT now();

ALTER TABLE panel_settings ALTER COLUMN auto_close_enabled           SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN auto_close_delay             SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN max_deep_save_mb             SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN delete_batch_size            SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN log_retention_days           SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN panel_timeout_seconds        SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN allow_multiple_panels        SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN reuse_existing_panel         SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN language                     SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN debug_callbacks              SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN owner_only                   SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN dashboard_font               SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN update_stale_seconds         SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN ghost_seen_retention_seconds SET NOT NULL;

-- panel_settings CHECK constraints. Every one is data-guarded: a legacy row
-- outside the documented range yields a WARNING naming the count instead of
-- aborting the whole script (mirrors settings_service._VALIDATORS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_auto_close_delay_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE auto_close_delay IS NULL OR auto_close_delay NOT BETWEEN 5 AND 3600) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_auto_close_delay_check
                CHECK (auto_close_delay BETWEEN 5 AND 3600);
        ELSE
            RAISE WARNING 'panel_settings_auto_close_delay_check NOT added - % row(s) have auto_close_delay outside 5..3600.',
                (SELECT count(*) FROM panel_settings WHERE auto_close_delay IS NULL OR auto_close_delay NOT BETWEEN 5 AND 3600);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_max_deep_save_mb_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE max_deep_save_mb IS NULL OR max_deep_save_mb NOT BETWEEN 1 AND 500) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_max_deep_save_mb_check
                CHECK (max_deep_save_mb BETWEEN 1 AND 500);
        ELSE
            RAISE WARNING 'panel_settings_max_deep_save_mb_check NOT added - % row(s) have max_deep_save_mb outside 1..500.',
                (SELECT count(*) FROM panel_settings WHERE max_deep_save_mb IS NULL OR max_deep_save_mb NOT BETWEEN 1 AND 500);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_delete_batch_size_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE delete_batch_size IS NULL OR delete_batch_size NOT BETWEEN 1 AND 1000) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_delete_batch_size_check
                CHECK (delete_batch_size BETWEEN 1 AND 1000);
        ELSE
            RAISE WARNING 'panel_settings_delete_batch_size_check NOT added - % row(s) have delete_batch_size outside 1..1000.',
                (SELECT count(*) FROM panel_settings WHERE delete_batch_size IS NULL OR delete_batch_size NOT BETWEEN 1 AND 1000);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_log_retention_days_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE log_retention_days IS NULL OR log_retention_days NOT BETWEEN 1 AND 365) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_log_retention_days_check
                CHECK (log_retention_days BETWEEN 1 AND 365);
        ELSE
            RAISE WARNING 'panel_settings_log_retention_days_check NOT added - % row(s) have log_retention_days outside 1..365.',
                (SELECT count(*) FROM panel_settings WHERE log_retention_days IS NULL OR log_retention_days NOT BETWEEN 1 AND 365);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_panel_timeout_seconds_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE panel_timeout_seconds IS NULL OR panel_timeout_seconds NOT BETWEEN 30 AND 86400) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_panel_timeout_seconds_check
                CHECK (panel_timeout_seconds BETWEEN 30 AND 86400);
        ELSE
            RAISE WARNING 'panel_settings_panel_timeout_seconds_check NOT added - % row(s) have panel_timeout_seconds outside 30..86400.',
                (SELECT count(*) FROM panel_settings WHERE panel_timeout_seconds IS NULL OR panel_timeout_seconds NOT BETWEEN 30 AND 86400);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_language_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE language IS NULL OR length(btrim(language)) = 0) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_language_check
                CHECK (length(btrim(language)) > 0);
        ELSE
            RAISE WARNING 'panel_settings_language_check NOT added - % row(s) have a blank language.',
                (SELECT count(*) FROM panel_settings WHERE language IS NULL OR length(btrim(language)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_dashboard_font_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE dashboard_font IS NULL OR dashboard_font NOT IN (
                'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                'script', 'script_bold', 'fraktur', 'fraktur_bold',
                'double_struck', 'mono', 'small_caps', 'circled',
                'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                'strikethrough', 'overline', 'wavy_underline'
            )) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_dashboard_font_check
                CHECK (dashboard_font IN (
                    'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                    'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                    'script', 'script_bold', 'fraktur', 'fraktur_bold',
                    'double_struck', 'mono', 'small_caps', 'circled',
                    'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                    'strikethrough', 'overline', 'wavy_underline'
                ));
        ELSE
            RAISE WARNING 'panel_settings_dashboard_font_check NOT added - % row(s) have a dashboard_font outside the 23-key list.',
                (SELECT count(*) FROM panel_settings WHERE dashboard_font IS NULL OR dashboard_font NOT IN (
                    'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                    'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                    'script', 'script_bold', 'fraktur', 'fraktur_bold',
                    'double_struck', 'mono', 'small_caps', 'circled',
                    'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                    'strikethrough', 'overline', 'wavy_underline'
                ));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_ghost_seen_retention_seconds_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE ghost_seen_retention_seconds IS NULL
                       OR NOT (ghost_seen_retention_seconds = 0
                               OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000)) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_ghost_seen_retention_seconds_check
                CHECK (ghost_seen_retention_seconds = 0
                       OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000);
        ELSE
            RAISE WARNING 'panel_settings_ghost_seen_retention_seconds_check NOT added - % row(s) have an out-of-range retention window.',
                (SELECT count(*) FROM panel_settings WHERE ghost_seen_retention_seconds IS NULL
                   OR NOT (ghost_seen_retention_seconds = 0
                           OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000));
        END IF;
    END IF;
END $$;

-- Required singleton row: repository updates target key='global' and
-- silently no-op without it. `ON CONFLICT DO NOTHING` is deliberately
-- targetless so it cannot fail on a table whose unique index on `key` was not
-- (yet) reconcilable.
INSERT INTO panel_settings (key) VALUES ('global')
ON CONFLICT DO NOTHING;

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
--
-- THE REPORTED PRODUCTION FAILURE. A live database whose bot_settings already
-- existed without `value_type` made the CREATE a silent no-op, so the INSERT
-- below raised ERROR 42703 (`column "value_type" of relation "bot_settings"
-- does not exist`). The reconciliation block fixes the class, not the symptom.
CREATE TABLE IF NOT EXISTS bot_settings (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    value_type  text        NOT NULL DEFAULT 'str',
    updated_at  timestamptz DEFAULT now()
);

ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS key        text;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS value      text;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS value_type text        NOT NULL DEFAULT 'str';
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

UPDATE bot_settings SET value      = ''    WHERE value      IS NULL;
UPDATE bot_settings SET value_type = 'str' WHERE value_type IS NULL;
UPDATE bot_settings SET updated_at = now() WHERE updated_at IS NULL;

ALTER TABLE bot_settings ALTER COLUMN value_type SET DEFAULT 'str';
ALTER TABLE bot_settings ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE bot_settings ALTER COLUMN value      SET NOT NULL;
ALTER TABLE bot_settings ALTER COLUMN value_type SET NOT NULL;

INSERT INTO bot_settings (key, value, value_type) VALUES
    ('auto_close_enabled', 'true', 'bool'),
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT DO NOTHING;

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
--
-- show_question / stt_model / stt_language / stt_passes are written by EVERY
-- config_store upsert payload (migrations 20260913000000 and 20260917000001).
-- They are therefore part of the canonical contract: omitting them made a
-- database built only from this script reject the whole upsert and silently
-- lose AI settings on restart.
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
    show_question   boolean      NOT NULL DEFAULT false,
    stt_model       text         DEFAULT NULL,
    stt_language    text         DEFAULT NULL,
    stt_passes      integer      NOT NULL DEFAULT 1,
    last_request_at timestamptz,
    last_latency_ms real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS id              bigserial;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS owner_id        bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS provider        text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS model           text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS temperature     real        DEFAULT 1.0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS max_tokens      integer     DEFAULT 4096;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS system_prompt   text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS history_budget  integer     DEFAULT 4000;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS is_configured   boolean     DEFAULT false;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS trigger_en      text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS trigger_fa      text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS show_question   boolean     NOT NULL DEFAULT false;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_model       text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_language    text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_passes      integer     NOT NULL DEFAULT 1;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS last_request_at timestamptz;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS last_latency_ms real        DEFAULT 0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS updated_at      timestamptz DEFAULT now();

UPDATE ai_config SET owner_id        = 0     WHERE owner_id        IS NULL;
UPDATE ai_config SET provider        = ''    WHERE provider        IS NULL;
UPDATE ai_config SET model           = ''    WHERE model           IS NULL;
UPDATE ai_config SET temperature     = 1.0   WHERE temperature     IS NULL;
UPDATE ai_config SET max_tokens      = 4096  WHERE max_tokens      IS NULL;
UPDATE ai_config SET system_prompt   = ''    WHERE system_prompt   IS NULL;
UPDATE ai_config SET history_budget  = 4000  WHERE history_budget  IS NULL;
UPDATE ai_config SET is_configured   = false WHERE is_configured   IS NULL;
UPDATE ai_config SET show_question   = false WHERE show_question   IS NULL;
UPDATE ai_config SET stt_passes      = 1     WHERE stt_passes      IS NULL;
UPDATE ai_config SET last_latency_ms = 0     WHERE last_latency_ms IS NULL;
UPDATE ai_config SET created_at      = now() WHERE created_at      IS NULL;
UPDATE ai_config SET updated_at      = now() WHERE updated_at      IS NULL;

ALTER TABLE ai_config ALTER COLUMN provider        SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN model           SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN temperature     SET DEFAULT 1.0;
ALTER TABLE ai_config ALTER COLUMN max_tokens      SET DEFAULT 4096;
ALTER TABLE ai_config ALTER COLUMN system_prompt   SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN history_budget  SET DEFAULT 4000;
ALTER TABLE ai_config ALTER COLUMN is_configured   SET DEFAULT false;
ALTER TABLE ai_config ALTER COLUMN show_question   SET DEFAULT false;
ALTER TABLE ai_config ALTER COLUMN stt_passes      SET DEFAULT 1;
ALTER TABLE ai_config ALTER COLUMN last_latency_ms SET DEFAULT 0;
ALTER TABLE ai_config ALTER COLUMN created_at      SET DEFAULT now();
ALTER TABLE ai_config ALTER COLUMN updated_at      SET DEFAULT now();

ALTER TABLE ai_config ALTER COLUMN owner_id      SET NOT NULL;
ALTER TABLE ai_config ALTER COLUMN show_question SET NOT NULL;
ALTER TABLE ai_config ALTER COLUMN stt_passes    SET NOT NULL;

-- stt_passes is the only ranged ai_config column (1..3, mirroring the AI
-- Settings control plane). Data-guarded like the panel_settings CHECKs.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_config_stt_passes_range') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_config WHERE stt_passes IS NULL OR stt_passes NOT BETWEEN 1 AND 3) THEN
            ALTER TABLE ai_config ADD CONSTRAINT ai_config_stt_passes_range
                CHECK (stt_passes BETWEEN 1 AND 3);
        ELSE
            RAISE WARNING 'ai_config_stt_passes_range NOT added - % row(s) have stt_passes outside 1..3.',
                (SELECT count(*) FROM ai_config WHERE stt_passes IS NULL OR stt_passes NOT BETWEEN 1 AND 3);
        END IF;
    END IF;
END $$;

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

ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS id            bigserial;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS session_id    text;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS owner_id      bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS provider      text        DEFAULT '';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS model         text        DEFAULT '';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS status        text        DEFAULT 'active';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS total_tokens  integer     DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS message_count integer     DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS created_at    timestamptz DEFAULT now();
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS updated_at    timestamptz DEFAULT now();

UPDATE ai_sessions SET session_id    = 'recovered-' || id WHERE session_id    IS NULL;
UPDATE ai_sessions SET owner_id      = 0         WHERE owner_id      IS NULL;
UPDATE ai_sessions SET provider      = ''        WHERE provider      IS NULL;
UPDATE ai_sessions SET model         = ''        WHERE model         IS NULL;
UPDATE ai_sessions SET status        = 'active'  WHERE status        IS NULL;
UPDATE ai_sessions SET total_tokens  = 0         WHERE total_tokens  IS NULL;
UPDATE ai_sessions SET message_count = 0         WHERE message_count IS NULL;
UPDATE ai_sessions SET created_at    = now()     WHERE created_at    IS NULL;
UPDATE ai_sessions SET updated_at    = now()     WHERE updated_at    IS NULL;

ALTER TABLE ai_sessions ALTER COLUMN provider      SET DEFAULT '';
ALTER TABLE ai_sessions ALTER COLUMN model         SET DEFAULT '';
ALTER TABLE ai_sessions ALTER COLUMN status        SET DEFAULT 'active';
ALTER TABLE ai_sessions ALTER COLUMN total_tokens  SET DEFAULT 0;
ALTER TABLE ai_sessions ALTER COLUMN message_count SET DEFAULT 0;
ALTER TABLE ai_sessions ALTER COLUMN created_at    SET DEFAULT now();
ALTER TABLE ai_sessions ALTER COLUMN updated_at    SET DEFAULT now();

ALTER TABLE ai_sessions ALTER COLUMN session_id SET NOT NULL;
ALTER TABLE ai_sessions ALTER COLUMN owner_id   SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_sessions_status_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_sessions WHERE status IS NULL OR status NOT IN ('active', 'completed', 'error', 'closed')) THEN
            ALTER TABLE ai_sessions ADD CONSTRAINT ai_sessions_status_check
                CHECK (status IN ('active', 'completed', 'error', 'closed'));
        ELSE
            RAISE WARNING 'ai_sessions_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_sessions WHERE status IS NULL OR status NOT IN ('active', 'completed', 'error', 'closed'));
        END IF;
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

ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS session_id  text;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS role        text        NOT NULL DEFAULT 'user';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS content     text        NOT NULL DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS token_count integer     DEFAULT 0;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS provider    text        DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS model       text        DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS created_at  timestamptz DEFAULT now();

UPDATE ai_messages SET session_id  = ''     WHERE session_id  IS NULL;
UPDATE ai_messages SET owner_id    = 0      WHERE owner_id    IS NULL;
UPDATE ai_messages SET role        = 'user' WHERE role        IS NULL;
UPDATE ai_messages SET content     = ''     WHERE content     IS NULL;
UPDATE ai_messages SET token_count = 0      WHERE token_count IS NULL;
UPDATE ai_messages SET provider    = ''     WHERE provider    IS NULL;
UPDATE ai_messages SET model       = ''     WHERE model       IS NULL;
UPDATE ai_messages SET created_at  = now()  WHERE created_at  IS NULL;

ALTER TABLE ai_messages ALTER COLUMN role        SET DEFAULT 'user';
ALTER TABLE ai_messages ALTER COLUMN content     SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN token_count SET DEFAULT 0;
ALTER TABLE ai_messages ALTER COLUMN provider    SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN model       SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN created_at  SET DEFAULT now();

ALTER TABLE ai_messages ALTER COLUMN session_id SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN owner_id   SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN role       SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN content    SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_messages_role_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_messages WHERE role IS NULL OR role NOT IN ('system', 'user', 'assistant', 'tool')) THEN
            ALTER TABLE ai_messages ADD CONSTRAINT ai_messages_role_check
                CHECK (role IN ('system', 'user', 'assistant', 'tool'));
        ELSE
            RAISE WARNING 'ai_messages_role_check NOT added - % row(s) have an unrecognized role.',
                (SELECT count(*) FROM ai_messages WHERE role IS NULL OR role NOT IN ('system', 'user', 'assistant', 'tool'));
        END IF;
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

ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS id         bigserial;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS owner_id   bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS tier       text        NOT NULL DEFAULT 'long';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS category   text        NOT NULL DEFAULT 'context';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS content    text;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS importance real        DEFAULT 0.5;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS metadata   jsonb       DEFAULT '{}';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

UPDATE ai_memories SET owner_id   = 0         WHERE owner_id   IS NULL;
UPDATE ai_memories SET tier       = 'long'    WHERE tier       IS NULL;
UPDATE ai_memories SET category   = 'context' WHERE category   IS NULL;
UPDATE ai_memories SET content    = ''        WHERE content    IS NULL;
UPDATE ai_memories SET importance = 0.5       WHERE importance IS NULL;
UPDATE ai_memories SET metadata   = '{}'      WHERE metadata   IS NULL;
UPDATE ai_memories SET created_at = now()     WHERE created_at IS NULL;

ALTER TABLE ai_memories ALTER COLUMN tier       SET DEFAULT 'long';
ALTER TABLE ai_memories ALTER COLUMN category   SET DEFAULT 'context';
ALTER TABLE ai_memories ALTER COLUMN importance SET DEFAULT 0.5;
ALTER TABLE ai_memories ALTER COLUMN metadata   SET DEFAULT '{}';
ALTER TABLE ai_memories ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE ai_memories ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN tier     SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN category SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN content  SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_tier_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_memories WHERE tier IS NULL OR tier NOT IN ('short', 'long', 'permanent')) THEN
            ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_tier_check
                CHECK (tier IN ('short', 'long', 'permanent'));
        ELSE
            RAISE WARNING 'ai_memories_tier_check NOT added - % row(s) have an unrecognized tier.',
                (SELECT count(*) FROM ai_memories WHERE tier IS NULL OR tier NOT IN ('short', 'long', 'permanent'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_category_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_memories WHERE category IS NULL OR category NOT IN ('fact', 'preference', 'context', 'summary', 'instruction')) THEN
            ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_category_check
                CHECK (category IN ('fact', 'preference', 'context', 'summary', 'instruction'));
        ELSE
            RAISE WARNING 'ai_memories_category_check NOT added - % row(s) have an unrecognized category.',
                (SELECT count(*) FROM ai_memories WHERE category IS NULL OR category NOT IN ('fact', 'preference', 'context', 'summary', 'instruction'));
        END IF;
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

ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS id             bigserial;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS owner_id       bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS session_id     text        DEFAULT '';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS tool_name      text;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS arguments      jsonb       DEFAULT '{}';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_success boolean     DEFAULT false;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_message text        DEFAULT '';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_data    jsonb       DEFAULT '{}';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS latency_ms     real        DEFAULT 0;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS created_at     timestamptz DEFAULT now();

UPDATE ai_tool_history SET owner_id       = 0         WHERE owner_id       IS NULL;
UPDATE ai_tool_history SET session_id     = ''        WHERE session_id     IS NULL;
UPDATE ai_tool_history SET tool_name      = 'unknown' WHERE tool_name      IS NULL;
UPDATE ai_tool_history SET arguments      = '{}'      WHERE arguments      IS NULL;
UPDATE ai_tool_history SET result_success = false     WHERE result_success IS NULL;
UPDATE ai_tool_history SET result_message = ''        WHERE result_message IS NULL;
UPDATE ai_tool_history SET result_data    = '{}'      WHERE result_data    IS NULL;
UPDATE ai_tool_history SET latency_ms     = 0         WHERE latency_ms     IS NULL;
UPDATE ai_tool_history SET created_at     = now()     WHERE created_at     IS NULL;

ALTER TABLE ai_tool_history ALTER COLUMN session_id     SET DEFAULT '';
ALTER TABLE ai_tool_history ALTER COLUMN arguments      SET DEFAULT '{}';
ALTER TABLE ai_tool_history ALTER COLUMN result_success SET DEFAULT false;
ALTER TABLE ai_tool_history ALTER COLUMN result_message SET DEFAULT '';
ALTER TABLE ai_tool_history ALTER COLUMN result_data    SET DEFAULT '{}';
ALTER TABLE ai_tool_history ALTER COLUMN latency_ms     SET DEFAULT 0;
ALTER TABLE ai_tool_history ALTER COLUMN created_at     SET DEFAULT now();

ALTER TABLE ai_tool_history ALTER COLUMN owner_id  SET NOT NULL;
ALTER TABLE ai_tool_history ALTER COLUMN tool_name SET NOT NULL;

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

ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS id                bigserial;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS owner_id          bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS session_id        text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS provider          text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS model             text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS prompt_tokens     integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS completion_tokens integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS total_tokens      integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS latency_ms        real        DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS token_source      text        DEFAULT NULL;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS created_at        timestamptz DEFAULT now();

UPDATE ai_usage SET owner_id          = 0     WHERE owner_id          IS NULL;
UPDATE ai_usage SET prompt_tokens     = 0     WHERE prompt_tokens     IS NULL;
UPDATE ai_usage SET completion_tokens = 0     WHERE completion_tokens IS NULL;
UPDATE ai_usage SET total_tokens      = 0     WHERE total_tokens      IS NULL;
UPDATE ai_usage SET latency_ms        = 0     WHERE latency_ms        IS NULL;
UPDATE ai_usage SET created_at        = now() WHERE created_at        IS NULL;

ALTER TABLE ai_usage ALTER COLUMN prompt_tokens     SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN completion_tokens SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN total_tokens      SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN latency_ms        SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN created_at        SET DEFAULT now();

ALTER TABLE ai_usage ALTER COLUMN owner_id SET NOT NULL;

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

ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS provider_name           text        NOT NULL DEFAULT '';
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS owner_id                bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_requests          integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS successful_requests     integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS failed_requests         integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_prompt_tokens     integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_completion_tokens integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS avg_latency_ms          real        NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS last_request_at         timestamptz;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS updated_at              timestamptz NOT NULL DEFAULT now();

UPDATE ai_provider_stats SET provider_name           = ''    WHERE provider_name           IS NULL;
UPDATE ai_provider_stats SET owner_id                = 0     WHERE owner_id                IS NULL;
UPDATE ai_provider_stats SET total_requests          = 0     WHERE total_requests          IS NULL;
UPDATE ai_provider_stats SET successful_requests     = 0     WHERE successful_requests     IS NULL;
UPDATE ai_provider_stats SET failed_requests         = 0     WHERE failed_requests         IS NULL;
UPDATE ai_provider_stats SET total_prompt_tokens     = 0     WHERE total_prompt_tokens     IS NULL;
UPDATE ai_provider_stats SET total_completion_tokens = 0     WHERE total_completion_tokens IS NULL;
UPDATE ai_provider_stats SET avg_latency_ms          = 0     WHERE avg_latency_ms          IS NULL;
UPDATE ai_provider_stats SET updated_at              = now() WHERE updated_at              IS NULL;

ALTER TABLE ai_provider_stats ALTER COLUMN provider_name           SET DEFAULT '';
ALTER TABLE ai_provider_stats ALTER COLUMN owner_id                SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_requests          SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN successful_requests     SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN failed_requests         SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_prompt_tokens     SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_completion_tokens SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN avg_latency_ms          SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN updated_at              SET DEFAULT now();

ALTER TABLE ai_provider_stats ALTER COLUMN provider_name           SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN owner_id                SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_requests          SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN successful_requests     SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN failed_requests         SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_prompt_tokens     SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_completion_tokens SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN avg_latency_ms          SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN updated_at              SET NOT NULL;

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

ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS chat_id         bigint;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS display_name    text        NOT NULL DEFAULT '';
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS last_preview    text        NOT NULL DEFAULT '';
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS last_message_at timestamptz;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS unread_count    integer     NOT NULL DEFAULT 0;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS updated_at      timestamptz DEFAULT now();

UPDATE ghost_chats SET display_name = ''    WHERE display_name IS NULL;
UPDATE ghost_chats SET last_preview = ''    WHERE last_preview IS NULL;
UPDATE ghost_chats SET unread_count = 0     WHERE unread_count IS NULL;
UPDATE ghost_chats SET created_at   = now() WHERE created_at   IS NULL;
UPDATE ghost_chats SET updated_at   = now() WHERE updated_at   IS NULL;

ALTER TABLE ghost_chats ALTER COLUMN display_name SET DEFAULT '';
ALTER TABLE ghost_chats ALTER COLUMN last_preview SET DEFAULT '';
ALTER TABLE ghost_chats ALTER COLUMN unread_count SET DEFAULT 0;
ALTER TABLE ghost_chats ALTER COLUMN created_at   SET DEFAULT now();
ALTER TABLE ghost_chats ALTER COLUMN updated_at   SET DEFAULT now();

ALTER TABLE ghost_chats ALTER COLUMN display_name SET NOT NULL;
ALTER TABLE ghost_chats ALTER COLUMN last_preview SET NOT NULL;
ALTER TABLE ghost_chats ALTER COLUMN unread_count SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ghost_chats_last_message
    ON ghost_chats (last_message_at DESC);

ALTER TABLE ghost_chats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_update_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_delete_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 16. ai_tasks ───────────────────────────────────────────────────────────
-- Created by supabase/migrations/20260829000001_create_ai_tasks.sql; the
-- canonical script previously omitted it even though the live task system
-- (backend/ai/task_scheduler.py, task_execution.py) depends on it.
-- schedule_type's CHECK includes 'event' — the final state produced by
-- 20260904000001_add_event_schedule_type.sql.
CREATE TABLE IF NOT EXISTS ai_tasks (
    id                         bigserial    PRIMARY KEY,
    owner_id                   bigint       NOT NULL,
    label                      text         NOT NULL,
    status                     text         NOT NULL DEFAULT 'active',
    version                    integer      NOT NULL DEFAULT 1,
    schedule_type              text         NOT NULL,
    schedule                   jsonb        NOT NULL,
    timezone                   text         NOT NULL,
    next_run_at                timestamptz,
    actions                    jsonb        NOT NULL,
    notification_destination   jsonb        NOT NULL,
    ai_instruction             text,
    created_at                 timestamptz  NOT NULL DEFAULT now(),
    updated_at                 timestamptz  NOT NULL DEFAULT now(),
    terminal_at                timestamptz,
    CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0),
    CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5),
    CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768),
    CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384),
    CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096),
    CONSTRAINT ai_tasks_ai_instruction_size
        CHECK (ai_instruction IS NULL OR (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384))
);

ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS id                       bigserial;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS owner_id                 bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS label                    text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS status                   text        NOT NULL DEFAULT 'active';
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS version                  integer     NOT NULL DEFAULT 1;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS schedule_type            text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS schedule                 jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS timezone                 text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS next_run_at              timestamptz;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS actions                  jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS notification_destination jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS ai_instruction           text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS created_at               timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS updated_at               timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS terminal_at              timestamptz;

-- Recovered rows cannot invent a real action list, so the empty array is the
-- deterministic sentinel; the array-count CHECK is then skipped with a
-- warning (see the guarded CHECK block below) rather than aborting the script.
UPDATE ai_tasks SET owner_id                 = 0           WHERE owner_id                 IS NULL;
UPDATE ai_tasks SET label                    = 'recovered' WHERE label                    IS NULL;
UPDATE ai_tasks SET status                   = 'active'    WHERE status                   IS NULL;
UPDATE ai_tasks SET version                  = 1           WHERE version                  IS NULL;
UPDATE ai_tasks SET schedule_type            = 'once'      WHERE schedule_type            IS NULL;
UPDATE ai_tasks SET schedule                 = '{}'        WHERE schedule                 IS NULL;
UPDATE ai_tasks SET timezone                 = 'UTC'       WHERE timezone                 IS NULL;
UPDATE ai_tasks SET actions                  = '[]'        WHERE actions                  IS NULL;
UPDATE ai_tasks SET notification_destination = '{}'        WHERE notification_destination IS NULL;
UPDATE ai_tasks SET created_at               = now()       WHERE created_at               IS NULL;
UPDATE ai_tasks SET updated_at               = now()       WHERE updated_at               IS NULL;

ALTER TABLE ai_tasks ALTER COLUMN status     SET DEFAULT 'active';
ALTER TABLE ai_tasks ALTER COLUMN version    SET DEFAULT 1;
ALTER TABLE ai_tasks ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE ai_tasks ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE ai_tasks ALTER COLUMN owner_id                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN label                    SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN status                   SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN version                  SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN schedule_type            SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN schedule                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN timezone                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN actions                  SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN notification_destination SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN created_at               SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN updated_at               SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_label_not_blank') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE length(btrim(label)) = 0) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0);
        ELSE
            RAISE WARNING 'ai_tasks_label_not_blank NOT added - % row(s) have a blank label.',
                (SELECT count(*) FROM ai_tasks WHERE length(btrim(label)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_status_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE status NOT IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_status_check
                CHECK (status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted'));
        ELSE
            RAISE WARNING 'ai_tasks_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_tasks WHERE status NOT IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_version_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE version <= 0) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_version_check CHECK (version > 0);
        ELSE
            RAISE WARNING 'ai_tasks_version_check NOT added - % row(s) have version <= 0.',
                (SELECT count(*) FROM ai_tasks WHERE version <= 0);
        END IF;
    END IF;
    -- schedule_type is DROP+ADD on purpose: a database whose constraint
    -- predates 20260904000001 still rejects the 'event' schedule type.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_tasks_schedule_type_check'
          AND pg_get_constraintdef(oid) LIKE '%event%'
    ) THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE schedule_type NOT IN ('once', 'interval', 'daily', 'weekly', 'event')) THEN
            ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
                CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));
        ELSE
            RAISE WARNING 'ai_tasks_schedule_type_check NOT replaced - % row(s) have an unrecognized schedule_type.',
                (SELECT count(*) FROM ai_tasks WHERE schedule_type NOT IN ('once', 'interval', 'daily', 'weekly', 'event'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_actions_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE jsonb_typeof(actions) IS DISTINCT FROM 'array') THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_check CHECK (jsonb_typeof(actions) = 'array');
        ELSE
            RAISE WARNING 'ai_tasks_actions_check NOT added - % row(s) have a non-array actions value.',
                (SELECT count(*) FROM ai_tasks WHERE jsonb_typeof(actions) IS DISTINCT FROM 'array');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_actions_count') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE jsonb_array_length(actions) NOT BETWEEN 1 AND 5) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5);
        ELSE
            RAISE WARNING 'ai_tasks_actions_count NOT added - % row(s) have an actions array outside 1..5 elements.',
                (SELECT count(*) FROM ai_tasks WHERE jsonb_array_length(actions) NOT BETWEEN 1 AND 5);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_payload_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(actions::text) > 32768) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768);
        ELSE
            RAISE WARNING 'ai_tasks_payload_size NOT added - % row(s) exceed 32768 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(actions::text) > 32768);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_schedule_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(schedule::text) > 16384) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384);
        ELSE
            RAISE WARNING 'ai_tasks_schedule_size NOT added - % row(s) exceed 16384 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(schedule::text) > 16384);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_destination_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(notification_destination::text) > 4096) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096);
        ELSE
            RAISE WARNING 'ai_tasks_destination_size NOT added - % row(s) exceed 4096 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(notification_destination::text) > 4096);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_ai_instruction_size') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_tasks
            WHERE ai_instruction IS NOT NULL
              AND NOT (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384)
        ) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_ai_instruction_size
                CHECK (ai_instruction IS NULL OR (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384));
        ELSE
            RAISE WARNING 'ai_tasks_ai_instruction_size NOT added - % row(s) have an empty or oversized ai_instruction.',
                (SELECT count(*) FROM ai_tasks
                   WHERE ai_instruction IS NOT NULL
                     AND NOT (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_tasks_status_next_run
    ON ai_tasks (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_ai_tasks_owner_updated
    ON ai_tasks (owner_id, updated_at DESC);

ALTER TABLE ai_tasks ENABLE ROW LEVEL SECURITY;

-- SELECT grant is restated because the task migration grants it explicitly
-- (20260829000001) rather than relying on schema default privileges.
GRANT SELECT ON ai_tasks TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_update_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_delete_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_select_ai_tasks" ON ai_tasks;
CREATE POLICY "anon_select_ai_tasks" ON ai_tasks FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 17. ai_task_occurrences ────────────────────────────────────────────────
-- Created by 20260829000001; `preparation_metadata` is the SECOND confirmed
-- instance of the drift class — the table's CREATE was a silent no-op on a
-- database whose table predated the column, so 20260912000001 exists as a
-- single-column repair. The reconciliation block below generalizes it.
CREATE TABLE IF NOT EXISTS ai_task_occurrences (
    id                    bigserial    PRIMARY KEY,
    task_id               bigint       NOT NULL REFERENCES ai_tasks(id) ON DELETE RESTRICT,
    owner_id              bigint       NOT NULL,
    occurrence_key        text         NOT NULL,
    definition_version    integer      NOT NULL,
    action_snapshot       jsonb        NOT NULL,
    scheduled_for         timestamptz  NOT NULL,
    attempt               smallint     NOT NULL DEFAULT 1,
    status                text         NOT NULL DEFAULT 'claimed',
    claimed_at            timestamptz,
    started_at            timestamptz,
    finished_at           timestamptz,
    retry_at              timestamptz,
    error_metadata        jsonb        NOT NULL DEFAULT '{}',
    result_metadata       jsonb        NOT NULL DEFAULT '{}',
    preparation_metadata  jsonb        NOT NULL DEFAULT '{}',
    created_at            timestamptz  NOT NULL DEFAULT now(),
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_task_occurrences_key_not_blank CHECK (length(btrim(occurrence_key)) > 0),
    CONSTRAINT ai_task_occurrences_action_count CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5),
    CONSTRAINT ai_task_occurrences_payload_size CHECK (octet_length(action_snapshot::text) <= 32768),
    CONSTRAINT ai_task_occurrences_error_metadata_check CHECK (jsonb_typeof(error_metadata) = 'object'),
    CONSTRAINT ai_task_occurrences_preparation_metadata_object CHECK (jsonb_typeof(preparation_metadata) = 'object'),
    CONSTRAINT ai_task_occurrences_error_size CHECK (octet_length(error_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_result_size CHECK (octet_length(result_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_preparation_size CHECK (octet_length(preparation_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_retry_state CHECK (
        (status = 'retry_pending' AND retry_at IS NOT NULL)
        OR (status <> 'retry_pending')
    )
);

ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS id                   bigserial;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS task_id              bigint;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS owner_id             bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS occurrence_key       text;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS definition_version   integer;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS action_snapshot      jsonb;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS scheduled_for        timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS attempt              smallint    NOT NULL DEFAULT 1;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS status               text        NOT NULL DEFAULT 'claimed';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS claimed_at           timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS started_at           timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS finished_at          timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS retry_at             timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS error_metadata       jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS result_metadata      jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS preparation_metadata jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS created_at           timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS updated_at           timestamptz NOT NULL DEFAULT now();

-- task_id points at an existing task when one exists, otherwise at the
-- deterministic sentinel 0; the foreign key is then added only when the data
-- actually resolves (see the guarded block below).
UPDATE ai_task_occurrences SET task_id              = COALESCE((SELECT min(id) FROM ai_tasks), 0) WHERE task_id              IS NULL;
UPDATE ai_task_occurrences SET owner_id             = 0                            WHERE owner_id             IS NULL;
UPDATE ai_task_occurrences SET occurrence_key       = 'recovered-' || id            WHERE occurrence_key       IS NULL;
UPDATE ai_task_occurrences SET definition_version   = 1                            WHERE definition_version   IS NULL;
UPDATE ai_task_occurrences SET action_snapshot      = '[]'                          WHERE action_snapshot      IS NULL;
UPDATE ai_task_occurrences SET scheduled_for        = now()                         WHERE scheduled_for        IS NULL;
UPDATE ai_task_occurrences SET attempt              = 1                             WHERE attempt              IS NULL;
UPDATE ai_task_occurrences SET status               = 'claimed'                     WHERE status               IS NULL;
UPDATE ai_task_occurrences SET error_metadata       = '{}'                          WHERE error_metadata       IS NULL;
UPDATE ai_task_occurrences SET result_metadata      = '{}'                          WHERE result_metadata      IS NULL;
UPDATE ai_task_occurrences SET preparation_metadata = '{}'                          WHERE preparation_metadata IS NULL;
UPDATE ai_task_occurrences SET created_at           = now()                         WHERE created_at           IS NULL;
UPDATE ai_task_occurrences SET updated_at           = now()                         WHERE updated_at           IS NULL;

ALTER TABLE ai_task_occurrences ALTER COLUMN attempt              SET DEFAULT 1;
ALTER TABLE ai_task_occurrences ALTER COLUMN status               SET DEFAULT 'claimed';
ALTER TABLE ai_task_occurrences ALTER COLUMN error_metadata       SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN result_metadata      SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN preparation_metadata SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN created_at           SET DEFAULT now();
ALTER TABLE ai_task_occurrences ALTER COLUMN updated_at           SET DEFAULT now();

ALTER TABLE ai_task_occurrences ALTER COLUMN task_id              SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN owner_id             SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN occurrence_key       SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN definition_version   SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN action_snapshot      SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN scheduled_for        SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN attempt              SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN status               SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN error_metadata       SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN result_metadata      SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN preparation_metadata SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN created_at           SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN updated_at           SET NOT NULL;

-- The task foreign key is data-guarded: it is added only when every row
-- resolves to an existing ai_tasks row.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_task_id_fkey') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences o
            LEFT JOIN ai_tasks t ON t.id = o.task_id
            WHERE t.id IS NULL
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_task_id_fkey
                FOREIGN KEY (task_id) REFERENCES ai_tasks(id) ON DELETE RESTRICT;
        ELSE
            RAISE WARNING 'ai_task_occurrences_task_id_fkey NOT added - % row(s) reference a task_id with no ai_tasks row. Resolve them and re-run this script.',
                (SELECT count(*) FROM ai_task_occurrences o
                   LEFT JOIN ai_tasks t ON t.id = o.task_id
                  WHERE t.id IS NULL);
        END IF;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_key_not_blank') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE length(btrim(occurrence_key)) = 0) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_key_not_blank
                CHECK (length(btrim(occurrence_key)) > 0);
        ELSE
            RAISE WARNING 'ai_task_occurrences_key_not_blank NOT added - % row(s) have a blank occurrence_key.',
                (SELECT count(*) FROM ai_task_occurrences WHERE length(btrim(occurrence_key)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_definition_version_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE definition_version <= 0) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_definition_version_check
                CHECK (definition_version > 0);
        ELSE
            RAISE WARNING 'ai_task_occurrences_definition_version_check NOT added - % row(s) have definition_version <= 0.',
                (SELECT count(*) FROM ai_task_occurrences WHERE definition_version <= 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_action_count') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_array_length(action_snapshot) NOT BETWEEN 1 AND 5) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_action_count
                CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5);
        ELSE
            RAISE WARNING 'ai_task_occurrences_action_count NOT added - % row(s) have an action_snapshot outside 1..5 elements.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_array_length(action_snapshot) NOT BETWEEN 1 AND 5);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_payload_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(action_snapshot::text) > 32768) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_payload_size
                CHECK (octet_length(action_snapshot::text) <= 32768);
        ELSE
            RAISE WARNING 'ai_task_occurrences_payload_size NOT added - % row(s) exceed 32768 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(action_snapshot::text) > 32768);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_attempt_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE attempt NOT BETWEEN 1 AND 3) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_attempt_check
                CHECK (attempt BETWEEN 1 AND 3);
        ELSE
            RAISE WARNING 'ai_task_occurrences_attempt_check NOT added - % row(s) have attempt outside 1..3.',
                (SELECT count(*) FROM ai_task_occurrences WHERE attempt NOT BETWEEN 1 AND 3);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_status_check') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            WHERE status NOT IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted')
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_status_check
                CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted'));
        ELSE
            RAISE WARNING 'ai_task_occurrences_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_task_occurrences
                  WHERE status NOT IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_error_metadata_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_typeof(error_metadata) IS DISTINCT FROM 'object') THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_error_metadata_check
                CHECK (jsonb_typeof(error_metadata) = 'object');
        ELSE
            RAISE WARNING 'ai_task_occurrences_error_metadata_check NOT added - % row(s) have a non-object error_metadata.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_typeof(error_metadata) IS DISTINCT FROM 'object');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_retry_state') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            WHERE NOT ((status = 'retry_pending' AND retry_at IS NOT NULL) OR (status <> 'retry_pending'))
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_retry_state CHECK (
                (status = 'retry_pending' AND retry_at IS NOT NULL)
                OR (status <> 'retry_pending')
            );
        ELSE
            RAISE WARNING 'ai_task_occurrences_retry_state NOT added - % row(s) have status=retry_pending without retry_at.',
                (SELECT count(*) FROM ai_task_occurrences
                  WHERE NOT ((status = 'retry_pending' AND retry_at IS NOT NULL) OR (status <> 'retry_pending')));
        END IF;
    END IF;
    -- The preparation_metadata pair is exactly what 20260912000001 adds.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_preparation_metadata_object') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_typeof(preparation_metadata) IS DISTINCT FROM 'object') THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
                CHECK (jsonb_typeof(preparation_metadata) = 'object');
        ELSE
            RAISE WARNING 'ai_task_occurrences_preparation_metadata_object NOT added - % row(s) have a non-object preparation_metadata.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_typeof(preparation_metadata) IS DISTINCT FROM 'object');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_error_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(error_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_error_size
                CHECK (octet_length(error_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_error_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(error_metadata::text) > 8192);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_result_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(result_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_result_size
                CHECK (octet_length(result_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_result_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(result_metadata::text) > 8192);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_preparation_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(preparation_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_preparation_size
                CHECK (octet_length(preparation_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_preparation_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(preparation_metadata::text) > 8192);
        END IF;
    END IF;
END $$;

-- Unique index: guarded because legacy occurrences may already collide on
-- (task_id, occurrence_key) — the occurrence ids are application-generated.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'uq_ai_task_occurrences_task_key') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            GROUP BY task_id, occurrence_key HAVING count(*) > 1
        ) THEN
            CREATE UNIQUE INDEX uq_ai_task_occurrences_task_key
                ON ai_task_occurrences (task_id, occurrence_key);
        ELSE
            RAISE WARNING 'ai_task_occurrences: uq_ai_task_occurrences_task_key NOT created - duplicate (task_id, occurrence_key) pairs exist. Resolve them and re-run this script.';
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_owner_scheduled
    ON ai_task_occurrences (owner_id, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_task_scheduled
    ON ai_task_occurrences (task_id, scheduled_for DESC);

ALTER TABLE ai_task_occurrences ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON ai_task_occurrences TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_update_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_delete_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_select_ai_task_occurrences" ON ai_task_occurrences;
CREATE POLICY "anon_select_ai_task_occurrences" ON ai_task_occurrences FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 18. Identity-constraint reconciliation ─────────────────────────────────
-- PRIMARY KEY / UNIQUE constraints are asserted here, after every column
-- exists, because PostgreSQL allows only ONE primary key per table and both
-- kinds require data that satisfies them. Each constraint is applied only when
-- the current rows can satisfy it; otherwise a WARNING names the table and
-- column so the owner can resolve it and re-run the script — the script itself
-- never aborts on pre-existing data.
--
-- kinds:  p = PRIMARY KEY (existence checked as "any PK on this table"),
--         u = UNIQUE      (existence checked by constraint name)
-- nullexpr / distinctexpr: the generic "no NULLs and no duplicates" predicate.
DO $$
DECLARE
    r record;
    ok boolean;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('saved_items',         'saved_items_pkey',            'p', 'id',                      'id',            'id'),
            ('saved_items',         'saved_items_save_code_key',   'u', 'save_code',               'save_code',     'save_code'),
            ('bio_state',           'bio_state_pkey',              'p', 'id',                      'id',            'id'),
            ('bio_state',           'bio_state_owner_id_key',      'u', 'owner_id',                'owner_id',      'owner_id'),
            ('username_state',      'username_state_pkey',         'p', 'id',                      'id',            'id'),
            ('username_state',      'username_state_owner_id_key', 'u', 'owner_id',                'owner_id',      'owner_id'),
            ('bot_logs',            'bot_logs_pkey',               'p', 'id',                      'id',            'id'),
            ('panel_settings',      'panel_settings_pkey',         'p', 'key',                     'key',           'key'),
            ('bot_settings',        'bot_settings_pkey',           'p', 'key',                     'key',           'key'),
            ('ai_config',           'ai_config_pkey',              'p', 'id',                      'id',            'id'),
            ('ai_config',           'ai_config_owner_id_key',      'u', 'owner_id',                'owner_id',      'owner_id'),
            ('ai_sessions',         'ai_sessions_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_sessions',         'ai_sessions_session_id_key',  'u', 'session_id',              'session_id',    'session_id'),
            ('ai_messages',         'ai_messages_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_memories',         'ai_memories_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_tool_history',     'ai_tool_history_pkey',        'p', 'id',                      'id',            'id'),
            ('ai_usage',            'ai_usage_pkey',               'p', 'id',                      'id',            'id'),
            ('ai_provider_stats',   'ai_provider_stats_pkey',      'p', 'provider_name, owner_id', 'provider_name', 'provider_name, owner_id'),
            ('ghost_chats',         'ghost_chats_pkey',            'p', 'chat_id',                 'chat_id',       'chat_id'),
            ('ai_tasks',            'ai_tasks_pkey',               'p', 'id',                      'id',            'id'),
            ('ai_task_occurrences', 'ai_task_occurrences_pkey',    'p', 'id',                      'id',            'id')
        ) AS v(tbl, cname, kind, cols, nullexpr, distinctexpr)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname = r.tbl
              AND (CASE WHEN r.kind = 'p' THEN c.contype = 'p' ELSE c.conname = r.cname END)
        ) THEN
            EXECUTE format('SELECT (count(*) = count(%s)) AND (count(DISTINCT %s) = count(*)) FROM %I',
                           r.nullexpr, r.distinctexpr, r.tbl) INTO ok;
            IF ok THEN
                EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I ' ||
                               CASE WHEN r.kind = 'p' THEN 'PRIMARY KEY (' ELSE 'UNIQUE (' END ||
                               r.cols || ')', r.tbl, r.cname);
            ELSE
                RAISE WARNING '%: constraint % NOT added - existing rows contain NULLs or duplicates in (%). Resolve them and re-run this script.',
                    r.tbl, r.cname, r.cols;
            END IF;
        END IF;
    END LOOP;
END $$;

-- ─── 19. PostgREST schema-cache reload ──────────────────────────────────────
-- Without this the API keeps serving the pre-reconciliation column list.
NOTIFY pgrst, 'reload schema';

COMMIT;

-- ============================================================================
-- Drift report — runs AFTER the commit, so it always reports the real state.
-- Zero rows means every canonical (table, column) now exists. Any row returned
-- names a column this script could not establish (create it manually).
-- ============================================================================
SELECT v.tbl || '.' || v.col AS missing_canonical_column
FROM (VALUES
    ('saved_items','id'), ('saved_items','save_code'), ('saved_items','save_type'),
    ('saved_items','origin_chat_id'), ('saved_items','origin_msg_id'),
    ('saved_items','saved_chat_id'), ('saved_items','saved_msg_id'),
    ('saved_items','sender_name'), ('saved_items','sender_id'), ('saved_items','mime_type'),
    ('saved_items','file_id'), ('saved_items','file_size'), ('saved_items','media_type'),
    ('saved_items','tags'), ('saved_items','caption'), ('saved_items','file_name'),
    ('saved_items','display_name'),
    ('saved_items','short_code'), ('saved_items','owner_id'), ('saved_items','created_at'),
    ('bio_state','id'), ('bio_state','owner_id'), ('bio_state','template'), ('bio_state','mood'),
    ('bio_state','custom_text'), ('bio_state','is_active'), ('bio_state','last_bio'), ('bio_state','updated_at'),
    ('username_state','id'), ('username_state','owner_id'), ('username_state','template'),
    ('username_state','mood'), ('username_state','custom_text'), ('username_state','is_active'),
    ('username_state','last_name'), ('username_state','updated_at'),
    ('bot_logs','id'), ('bot_logs','owner_id'), ('bot_logs','level'), ('bot_logs','message'),
    ('bot_logs','context'), ('bot_logs','created_at'),
    ('panel_settings','key'), ('panel_settings','auto_close_enabled'), ('panel_settings','auto_close_delay'),
    ('panel_settings','max_deep_save_mb'), ('panel_settings','delete_batch_size'),
    ('panel_settings','log_retention_days'), ('panel_settings','panel_timeout_seconds'),
    ('panel_settings','allow_multiple_panels'), ('panel_settings','reuse_existing_panel'),
    ('panel_settings','language'), ('panel_settings','debug_callbacks'), ('panel_settings','owner_only'),
    ('panel_settings','dashboard_font'), ('panel_settings','update_stale_seconds'),
    ('panel_settings','ghost_seen_retention_seconds'), ('panel_settings','updated_at'),
    ('bot_settings','key'), ('bot_settings','value'), ('bot_settings','value_type'), ('bot_settings','updated_at'),
    ('ai_config','id'), ('ai_config','owner_id'), ('ai_config','provider'), ('ai_config','model'),
    ('ai_config','temperature'), ('ai_config','max_tokens'), ('ai_config','system_prompt'),
    ('ai_config','history_budget'), ('ai_config','is_configured'), ('ai_config','trigger_en'),
    ('ai_config','trigger_fa'), ('ai_config','show_question'), ('ai_config','stt_model'),
    ('ai_config','stt_language'), ('ai_config','stt_passes'), ('ai_config','last_request_at'),
    ('ai_config','last_latency_ms'), ('ai_config','created_at'), ('ai_config','updated_at'),
    ('ai_sessions','id'), ('ai_sessions','session_id'), ('ai_sessions','owner_id'),
    ('ai_sessions','provider'), ('ai_sessions','model'), ('ai_sessions','status'),
    ('ai_sessions','total_tokens'), ('ai_sessions','message_count'), ('ai_sessions','created_at'),
    ('ai_sessions','updated_at'),
    ('ai_messages','id'), ('ai_messages','session_id'), ('ai_messages','owner_id'),
    ('ai_messages','role'), ('ai_messages','content'), ('ai_messages','token_count'),
    ('ai_messages','provider'), ('ai_messages','model'), ('ai_messages','created_at'),
    ('ai_memories','id'), ('ai_memories','owner_id'), ('ai_memories','tier'), ('ai_memories','category'),
    ('ai_memories','content'), ('ai_memories','importance'), ('ai_memories','expires_at'),
    ('ai_memories','metadata'), ('ai_memories','created_at'),
    ('ai_tool_history','id'), ('ai_tool_history','owner_id'), ('ai_tool_history','session_id'),
    ('ai_tool_history','tool_name'), ('ai_tool_history','arguments'), ('ai_tool_history','result_success'),
    ('ai_tool_history','result_message'), ('ai_tool_history','result_data'),
    ('ai_tool_history','latency_ms'), ('ai_tool_history','created_at'),
    ('ai_usage','id'), ('ai_usage','owner_id'), ('ai_usage','session_id'), ('ai_usage','provider'),
    ('ai_usage','model'), ('ai_usage','prompt_tokens'), ('ai_usage','completion_tokens'),
    ('ai_usage','total_tokens'), ('ai_usage','latency_ms'), ('ai_usage','token_source'),
    ('ai_usage','created_at'),
    ('ai_provider_stats','provider_name'), ('ai_provider_stats','owner_id'),
    ('ai_provider_stats','total_requests'), ('ai_provider_stats','successful_requests'),
    ('ai_provider_stats','failed_requests'), ('ai_provider_stats','total_prompt_tokens'),
    ('ai_provider_stats','total_completion_tokens'), ('ai_provider_stats','avg_latency_ms'),
    ('ai_provider_stats','last_request_at'), ('ai_provider_stats','updated_at'),
    ('ghost_chats','chat_id'), ('ghost_chats','display_name'), ('ghost_chats','last_preview'),
    ('ghost_chats','last_message_at'), ('ghost_chats','unread_count'),
    ('ghost_chats','created_at'), ('ghost_chats','updated_at'),
    ('ai_tasks','id'), ('ai_tasks','owner_id'), ('ai_tasks','label'), ('ai_tasks','status'),
    ('ai_tasks','version'), ('ai_tasks','schedule_type'), ('ai_tasks','schedule'),
    ('ai_tasks','timezone'), ('ai_tasks','next_run_at'), ('ai_tasks','actions'),
    ('ai_tasks','notification_destination'), ('ai_tasks','ai_instruction'),
    ('ai_tasks','created_at'), ('ai_tasks','updated_at'), ('ai_tasks','terminal_at'),
    ('ai_task_occurrences','id'), ('ai_task_occurrences','task_id'), ('ai_task_occurrences','owner_id'),
    ('ai_task_occurrences','occurrence_key'), ('ai_task_occurrences','definition_version'),
    ('ai_task_occurrences','action_snapshot'), ('ai_task_occurrences','scheduled_for'),
    ('ai_task_occurrences','attempt'), ('ai_task_occurrences','status'),
    ('ai_task_occurrences','claimed_at'), ('ai_task_occurrences','started_at'),
    ('ai_task_occurrences','finished_at'), ('ai_task_occurrences','retry_at'),
    ('ai_task_occurrences','error_metadata'), ('ai_task_occurrences','result_metadata'),
    ('ai_task_occurrences','preparation_metadata'), ('ai_task_occurrences','created_at'),
    ('ai_task_occurrences','updated_at')
) AS v(tbl, col)
LEFT JOIN information_schema.columns c
    ON c.table_schema = 'public' AND c.table_name = v.tbl AND c.column_name = v.col
WHERE c.column_name IS NULL
ORDER BY 1;
