/*
# Anti-Edit / Anti-Delete: the two dedicated archive tables

Anti-Edit / Anti-Delete (AED) archived the owner's PRIVATE conversations into
the generic `bot_settings` key/value document: ONE configuration document
(`anti_edit_delete_config:<owner>`) plus a small fixed set of JSON shard
documents (`anti_edit_delete_archive_shard:0..3`) holding the rows. This
migration gives the feature its own two tables in their FINAL shape — exactly
the shape `backend/services/anti_edit_delete_service.py` reads and writes — so
the archive no longer shares one row-per-shard document with every other
`bot_settings` consumer. Like every successor migration it is forward-only and
additive: it creates two tables, their constraints, one index, RLS policies,
grants and comments, and it touches no existing table, column, row, policy or
grant.

    aed_config           the owner's configuration (ONE row per owner)
    aed_archive_entries  one row per observed archive record

## Columns

### aed_config
- `owner_id`       — PK: exactly ONE configuration row per owner.
- `enabled`        — the master switch; NOT NULL DEFAULT `false`, so applying
                     this migration can never start archiving by itself.
- `categories`     — `text[]`: the content-category filter the panel edits. The
                     service never stores an empty filter (it refuses to archive
                     nothing), so the column defaults to the full vocabulary.
- `retention_days` — the retention window the panel enforces (1..3650).
- `updated_at`     — durable timestamp.

### aed_archive_entries
- `id`             — PK: the deterministic archive id — bare lowercase hex, 16
                     chars — which is also the idempotency key of one
                     redelivered update.
- `owner_id`       — owning Telegram user. Every repository read/write filters
                     by it.
- `chat_id`        — the POSITIVE private-chat id (the other participant's id).
                     The CHECK (`> 0`) makes it structurally impossible to store
                     a group/supergroup/channel row (their ids are negative):
                     the archive scope cannot be broadened by a bad write.
- `message_id`     — the Telegram message id within that chat.
- `kind`           — `snapshot` / `edit` / `delete`.
- `category`       — the row's single primary content category.
- `has_caption`    — whether the media carried a text caption.
- `captured_at`    — when the archive observed the row (retention reads it).
- `message_date`   — the message's own Telegram date, when known.
- `edited_at`      — the edit date of an edit version, when known.
- `sender_id`      — the resolved sender's id (0 when unknown).
- `out`            — whether the message was outgoing from the owner.
- `has_media`      — whether the message carried media.
- `body`           — the bounded message text / caption (CHECK `<= 4096`, the
                     service's own text bound).
- `media_type`     — bounded media type label, when known.
- `media_name`     — bounded file name, when known.
- `media_size`     — bounded media size in bytes, when known.
- `media_stored`   — whether a media snapshot object was uploaded.
- `media_object`   — the storage object path, when one was uploaded.
- `recoverable`    — a deletion is recoverable ONLY when its content was stored
                     beforehand; a miss is recorded honestly as `false`.
- `recovered_from` — the id of the content version a deletion recovered from.
- `peer_id`        — the other participant's id (equals `chat_id` in a private
                     chat).
- `peer_username`  — the peer's public username without the sigil, when
                     observed; never inferred.
- `peer_name`      — the peer's display name, when observed; never an identifier.
- `updated_at`     — durable timestamp.

## Retention and scope (unchanged, and NOT schema-level choices)

Retention is read per owner from `aed_config.retention_days` and applied by the
EXISTING periodic memory-cleanup worker, which calls
`anti_edit_delete_service.cleanup_expired` — no scheduler is added. The scope
(genuine private human-to-human conversations only) stays enforced in code:
the capture gate excludes groups/channels (negative ids), Saved Messages
(`chat_id == owner_id`), inline-bot messages and resolved Telegram bots. The
`chat_id > 0` CHECK is the one structural backstop, and it is deliberately the
only scope-shaped constraint: the human-vs-bot decision is code, not data.

## Transition from the legacy `bot_settings` shards

This migration does NOT move any existing record. The dedicated tables become
the authoritative store for every NEW write; the legacy shard documents are
left exactly as the owner left them and are read READ-ONLY only while the
dedicated store is unavailable (i.e. before this migration is applied), so the
feature keeps working before the owner runs it. No row is copied, rewritten or
deleted by this file. The safe migration of the legacy records is a SEPARATE,
later task; until it runs, records written before this migration are not shown
once the tables exist.

## Security

RLS is enabled on both tables and `anon`/`authenticated` receive SELECT only —
the same read-only dashboard boundary every other table has. Every write goes
through the backend service-role client, and every repository query filters by
`owner_id`, so one owner can never read or mutate another owner's rows. The AI
has no tool that reaches either table: capture, browsing and cleanup are
deterministic service code.

Idempotent: safe to run more than once. Applying it to the live Supabase project
is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file (it is part 11 of the ONE complete setup script in
DATABASE_ARCHITECTURE.md §31.3, so it is normally applied by running that block
once). Until it is applied the feature keeps its current behaviour: the archive
is read and written through the legacy `bot_settings` documents, which this
migration neither reads nor modifies.

Rollback (destroys the dedicated configuration and archive rows — the feature
then reports honestly that its durable store is missing and stays OFF; the
legacy `bot_settings` documents are NOT touched, so the pre-migration archive
is still there):

    DROP TABLE IF EXISTS aed_archive_entries;
    DROP TABLE IF EXISTS aed_config;

Nothing else in the schema changes: no existing table, column, index, policy,
grant or row is touched.
*/

CREATE TABLE IF NOT EXISTS aed_config (
    owner_id       bigint      PRIMARY KEY,
    enabled        boolean     NOT NULL DEFAULT false,
    categories     text[]      NOT NULL DEFAULT '{text,captions,photos,videos,voice,documents,other}',
    retention_days integer     NOT NULL DEFAULT 30,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aed_config_retention_range
        CHECK (retention_days BETWEEN 1 AND 3650)
);

CREATE TABLE IF NOT EXISTS aed_archive_entries (
    id             text        PRIMARY KEY,
    owner_id       bigint      NOT NULL,
    chat_id        bigint      NOT NULL,
    message_id     bigint      NOT NULL,
    kind           text        NOT NULL,
    category       text        NOT NULL,
    has_caption    boolean     NOT NULL DEFAULT false,
    captured_at    timestamptz NOT NULL DEFAULT now(),
    message_date   timestamptz,
    edited_at      timestamptz,
    sender_id      bigint      NOT NULL DEFAULT 0,
    out            boolean     NOT NULL DEFAULT false,
    has_media      boolean     NOT NULL DEFAULT false,
    body           text        NOT NULL DEFAULT '',
    media_type     text,
    media_name     text,
    media_size     bigint,
    media_stored   boolean     NOT NULL DEFAULT false,
    media_object   text,
    recoverable    boolean     NOT NULL DEFAULT false,
    recovered_from text,
    peer_id        bigint,
    peer_username  text,
    peer_name      text,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT aed_archive_entries_kind_valid
        CHECK (kind IN ('snapshot', 'edit', 'delete')),
    CONSTRAINT aed_archive_entries_category_valid
        CHECK (category IN ('text', 'captions', 'photos', 'videos',
                            'voice', 'documents', 'other')),
    CONSTRAINT aed_archive_entries_chat_positive
        CHECK (chat_id > 0),
    CONSTRAINT aed_archive_entries_message_positive
        CHECK (message_id > 0),
    CONSTRAINT aed_archive_entries_body_length
        CHECK (length(body) <= 4096)
);

CREATE INDEX IF NOT EXISTS idx_aed_archive_entries_owner_captured
    ON aed_archive_entries (owner_id, captured_at DESC);

ALTER TABLE aed_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE aed_archive_entries ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON aed_config TO anon, authenticated;
GRANT SELECT ON aed_archive_entries TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_aed_config" ON aed_config;
DROP POLICY IF EXISTS "anon_update_aed_config" ON aed_config;
DROP POLICY IF EXISTS "anon_delete_aed_config" ON aed_config;
DROP POLICY IF EXISTS "anon_select_aed_config" ON aed_config;
CREATE POLICY "anon_select_aed_config" ON aed_config FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_aed_archive_entries" ON aed_archive_entries;
DROP POLICY IF EXISTS "anon_update_aed_archive_entries" ON aed_archive_entries;
DROP POLICY IF EXISTS "anon_delete_aed_archive_entries" ON aed_archive_entries;
DROP POLICY IF EXISTS "anon_select_aed_archive_entries" ON aed_archive_entries;
CREATE POLICY "anon_select_aed_archive_entries" ON aed_archive_entries FOR SELECT
    TO anon, authenticated USING (true);

COMMENT ON TABLE aed_config IS
    'ONE Anti-Edit / Anti-Delete configuration row per owner. The feature defaults to OFF; categories is the content filter (never empty) and retention_days (1..3650) is applied by the existing periodic memory-cleanup worker, not by a scheduler.';
COMMENT ON COLUMN aed_config.categories IS
    'The content-category filter the panel edits. The service refuses to store an empty filter (an empty filter would archive nothing).';
COMMENT ON COLUMN aed_config.retention_days IS
    'Retention window in days (1..3650). Read per owner by anti_edit_delete_service.cleanup_expired.';
COMMENT ON TABLE aed_archive_entries IS
    'One Anti-Edit / Anti-Delete archive record: a bounded snapshot of a private message, an observed edit version, or an honest deletion event. Only genuine private human-to-human conversations are archived — the scope is enforced in code and the chat_id > 0 CHECK is its structural backstop.';
COMMENT ON COLUMN aed_archive_entries.id IS
    'Deterministic archive id (bare lowercase hex, 16 chars) — the idempotency key of one redelivered update; the primary key.';
COMMENT ON COLUMN aed_archive_entries.kind IS
    'snapshot (new message), edit (an observed edit version) or delete (a deletion event). A deletion is recoverable only when its content was stored beforehand.';
COMMENT ON COLUMN aed_archive_entries.recoverable IS
    'TRUE only for a deletion whose original content was observed and stored before the deletion; a miss is recorded honestly as FALSE and never fabricated.';
COMMENT ON COLUMN aed_archive_entries.media_object IS
    'Storage object path of a bounded media snapshot, when one was uploaded; NULL when only media metadata is retained (the default).';
COMMENT ON COLUMN aed_archive_entries.chat_id IS
    'The POSITIVE private-chat id (the other participant''s id). The CHECK (> 0) makes storing a group/supergroup/channel row (negative ids) impossible.';

NOTIFY pgrst, 'reload schema';

SELECT 'anti edit delete tables' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('aed_config', 'aed_archive_entries');

SELECT 'aed_config columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'aed_config'
  AND column_name IN ('owner_id', 'enabled', 'categories', 'retention_days',
                      'updated_at');

SELECT 'aed_archive_entries columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'aed_archive_entries'
  AND column_name IN ('id', 'owner_id', 'chat_id', 'message_id', 'kind',
                      'category', 'has_caption', 'captured_at', 'message_date',
                      'edited_at', 'sender_id', 'out', 'has_media', 'body',
                      'media_type', 'media_name', 'media_size', 'media_stored',
                      'media_object', 'recoverable', 'recovered_from', 'peer_id',
                      'peer_username', 'peer_name', 'updated_at');

-- Read-only legacy probe: the documents this feature reads READ-ONLY while the
-- dedicated store above is unavailable. A row here means the pre-migration
-- archive is still present (nothing is modified by this query).
SELECT 'legacy anti edit delete settings' AS check, key
FROM bot_settings
WHERE key LIKE 'anti_edit_delete%';
