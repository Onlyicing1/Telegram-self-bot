/*
# Emoji & Reaction: the five persistence tables of the Emoji & Reaction feature

The Emoji & Reaction feature (premium/custom emoji replacement + reactions)
stores its library, categories, mappings and replacement state in five
dedicated tables. This migration creates them in their FINAL shape. Like every
successor migration it is forward-only and additive: it creates tables,
constraints, indexes, RLS policies, grants and comments, and it touches no
existing table, column, index, row, policy or grant.

    emoji_library            the owner's premium-emoji definitions
    emoji_categories         the owner's categories (ordinary + Custom)
    emoji_mappings           simple emoji → library entry, per category
    emoji_state              one global replacement-state row per owner
    emoji_chat_overrides     per-chat category overrides (IMPLEMENTED, but NOT
                             owner-approved — see "Owner decisions" below)

## Columns

### emoji_library
- `id`            — surrogate identity (bigserial PK).
- `owner_id`      — the owning Telegram user. Every repository read/write
                    filters by it; the service layer never crosses owners.
- `document_id`   — the Telegram custom-emoji document id. `UNIQUE
                    (owner_id, document_id)` is the deduplication identity the
                    importer and the manual entry paths rely on; the durable
                    unique index is the backstop for libraries larger than the
                    bounded dedup read.
- `alt_text`      — the Unicode fallback text Telegram provides for that
                    document (may be empty). Stored verbatim; never invented.
- `source`        — `imported` (the only producer today) or `manual` (the
                    documented, currently unproduced second source). A CHECK
                    keeps the value inside the documented domain.
- `source_msg_id` — the Saved Messages message the entry was imported from, or
                    NULL for entries collected through sticker-set enumeration
                    (those carry no message origin).
- `created_at`    — durable timestamp; the library browser reads newest-first.

### emoji_categories
- `id`                     — surrogate identity (bigserial PK).
- `owner_id`               — owning Telegram user.
- `name`                   — the owner's own label, nonblank, ≤ 64 characters
                             (the service's `MAX_CATEGORY_NAME_LEN`); `UNIQUE
                             (owner_id, name)` is the uniqueness contract the
                             service checks against.
- `is_custom`              — the EXPLICIT Custom-Category type flag (Phase 5).
                             Never inferred from the name. Ordinary rows are
                             inserted without this column and take the default.
- `source_category_ids`    — ordered id list of a Custom Category's sources
                             (Phase 5 snapshot-on-compose). `[]` for ordinary
                             categories; the array CHECK keeps the column a
                             JSONB array so the reader can never see another
                             shape.
- `created_at` / `updated_at` — durable timestamps.

### emoji_mappings
- `id`            — surrogate identity (bigserial PK).
- `owner_id`      — owning Telegram user.
- `category_id`   — application-level reference to `emoji_categories.id`
                    (owner-scoped, validated by the service before every
                    write). Deliberately NOT a foreign key: this database's
                    documented model keeps identifier relationships
                    application-level, and Phase 1–5 behaviour fails closed on
                    a dangling reference instead of cascading silently.
- `simple_emoji`  — the mapping KEY the owner types, nonblank, ≤ 32 characters
                    (`MAX_SIMPLE_EMOJI_LEN`); the emoji semantics are
                    Telegram's business at replacement time.
- `document_id`   — application-level reference to a library entry's
                    `document_id`. Mappings reference definitions; they never
                    duplicate them.
- `created_at` / `updated_at` — durable timestamps.
- `UNIQUE (owner_id, category_id, simple_emoji)` — one definition per simple
                    emoji per category: a shared emoji is never silently
                    overwritten (the service resolves the conflict explicitly).

### emoji_state
- `owner_id`                   — PK: exactly ONE global state row per owner.
- `replacement_enabled`        — the replacement toggle; NOT NULL DEFAULT
                                 `false` — the feature is OFF on first boot.
- `global_default_category_id` — the owner's global default category
                                 (application-level reference; NULL means none).
- `updated_at`                 — durable timestamp.

### emoji_chat_overrides
- `owner_id`             — owning Telegram user.
- `chat_id`              — the chat the override applies to.
- `override_category_id` — the override category (application-level reference;
                           NULL restores the global default at resolution
                           time).
- `updated_at`           — durable timestamp.
- `PRIMARY KEY (owner_id, chat_id)` — at most one override row per owner/chat.

## Owner decisions (recorded 2026-10-07)

The owner has approved the FINAL product behaviour of this feature
(`ROADMAP.md` §34): the **Active Category scope is GLOBAL-ONLY** — there is no
per-chat active-category override in the approved behaviour, and the effective
category is resolved from the global default only. Decisions B–G and J of §34
(send-first reconstruction, existing bridge bot, custom-emoji entities through
the bridge, dedicated mapping persistence, snapshot-on-compose, structural loop
prevention, the reply-mode reaction UX) are approved as implemented.

`emoji_chat_overrides` therefore holds state for a code path that remains
IMPLEMENTED but is NOT owner-approved: it is created here (the current code
still reads and writes it, and this migration is additive-only), and it is
documented as implemented-but-not-approved rather than silently removed. The
table is not part of the approved resolution order — with the global-only
decision the approved behaviour never consults an override. No removal
migration is invented; if the owner later wants the code path gone, that is a
separate, explicitly requested change (code + documentation + migration).

## Constraints and indexes

Every constraint is declared inline in the CREATE (these tables have no legacy
shape; the two Phase 5 columns are re-asserted with
`ADD COLUMN IF NOT EXISTS` so that a manually created Phase-2-shaped
`emoji_categories` still converges to the final shape). The indexes are exactly
the ones the repository's reads need, no more:

- `UNIQUE (owner_id, document_id)` on `emoji_library` — the dedup identity and
  the entry lookup.
- `(owner_id, created_at DESC)` on `emoji_library` — the newest-first library
  browser (`list_emoji_entries`).
- `UNIQUE (owner_id, name)` on `emoji_categories` — name uniqueness and the
  by-name lookup.
- `(owner_id, created_at DESC)` on `emoji_categories` — the categories grid
  (`list_emoji_categories`).
- `UNIQUE (owner_id, category_id, simple_emoji)` on `emoji_mappings` — the
  one-definition-per-emoji contract.
- `(owner_id, category_id, created_at DESC)` on `emoji_mappings` — the paged
  per-category mapping list and the owner-scoped per-category counts.
- `emoji_state` (PK `owner_id`) and `emoji_chat_overrides` (PK
  `(owner_id, chat_id)`) need no extra index: every read is a primary-key
  lookup.

## Security

RLS is enabled on all five tables and `anon`/`authenticated` receive SELECT
only — the same read-only dashboard boundary every other table has. Every write
goes through the backend's service-role client, and the repository filters by
`owner_id`, so one owner can never read or mutate another owner's rows. The AI
never reaches these tables: the replacement/import/reaction code is
deterministic service code, and the AI has no tool that writes them.

## Reactions

Reactions (Phase 6) persist NOTHING: a reaction is an action, not
configuration. This migration therefore creates no reaction table, and the
canonical schema contains none.

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply this file (it is part 9 of the ONE complete setup script in
DATABASE_ARCHITECTURE.md §31.3, so it is normally applied by running that block
once). Rollback (destroys the emoji library, categories, mappings and state —
the feature then reports honestly that its durable store is missing and keeps
running on the in-memory fallback; no other table is affected):

    DROP TABLE IF EXISTS emoji_chat_overrides;
    DROP TABLE IF EXISTS emoji_state;
    DROP TABLE IF EXISTS emoji_mappings;
    DROP TABLE IF EXISTS emoji_categories;
    DROP TABLE IF EXISTS emoji_library;

Nothing else in the schema changes: no existing table, column, index, policy,
grant or row is touched.
*/

CREATE TABLE IF NOT EXISTS emoji_library (
    id            bigserial   PRIMARY KEY,
    owner_id      bigint      NOT NULL,
    document_id   bigint      NOT NULL,
    alt_text      text        NOT NULL DEFAULT '',
    source        text        NOT NULL DEFAULT 'imported',
    source_msg_id bigint,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT emoji_library_document_positive
        CHECK (document_id > 0),
    CONSTRAINT emoji_library_source_check
        CHECK (source IN ('imported', 'manual')),
    CONSTRAINT emoji_library_owner_document_key
        UNIQUE (owner_id, document_id)
);

CREATE TABLE IF NOT EXISTS emoji_categories (
    id                  bigserial   PRIMARY KEY,
    owner_id            bigint      NOT NULL,
    name                text        NOT NULL,
    is_custom           boolean     NOT NULL DEFAULT false,
    source_category_ids jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT emoji_categories_name_not_blank
        CHECK (length(btrim(name)) > 0 AND length(name) <= 64),
    CONSTRAINT emoji_categories_source_ids_array
        CHECK (jsonb_typeof(source_category_ids) = 'array'),
    CONSTRAINT emoji_categories_owner_name_key
        UNIQUE (owner_id, name)
);

-- The Phase 5 Custom-Category columns, re-asserted so a manually created
-- Phase-2-shaped table converges to the final shape instead of silently
-- keeping two columns fewer.
ALTER TABLE emoji_categories
    ADD COLUMN IF NOT EXISTS is_custom boolean NOT NULL DEFAULT false;
ALTER TABLE emoji_categories
    ADD COLUMN IF NOT EXISTS source_category_ids jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS emoji_mappings (
    id           bigserial   PRIMARY KEY,
    owner_id     bigint      NOT NULL,
    category_id  bigint      NOT NULL,
    simple_emoji text        NOT NULL,
    document_id  bigint      NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT emoji_mappings_simple_emoji_not_blank
        CHECK (length(btrim(simple_emoji)) > 0 AND length(simple_emoji) <= 32),
    CONSTRAINT emoji_mappings_document_positive
        CHECK (document_id > 0),
    CONSTRAINT emoji_mappings_owner_category_emoji_key
        UNIQUE (owner_id, category_id, simple_emoji)
);

CREATE TABLE IF NOT EXISTS emoji_state (
    owner_id                   bigint      PRIMARY KEY,
    replacement_enabled        boolean     NOT NULL DEFAULT false,
    global_default_category_id bigint,
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS emoji_chat_overrides (
    owner_id             bigint      NOT NULL,
    chat_id              bigint      NOT NULL,
    override_category_id bigint,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_emoji_library_owner_created
    ON emoji_library (owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_emoji_categories_owner_created
    ON emoji_categories (owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_emoji_mappings_owner_category_created
    ON emoji_mappings (owner_id, category_id, created_at DESC);

ALTER TABLE emoji_library ENABLE ROW LEVEL SECURITY;
ALTER TABLE emoji_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE emoji_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE emoji_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE emoji_chat_overrides ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON emoji_library TO anon, authenticated;
GRANT SELECT ON emoji_categories TO anon, authenticated;
GRANT SELECT ON emoji_mappings TO anon, authenticated;
GRANT SELECT ON emoji_state TO anon, authenticated;
GRANT SELECT ON emoji_chat_overrides TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_emoji_library" ON emoji_library;
DROP POLICY IF EXISTS "anon_update_emoji_library" ON emoji_library;
DROP POLICY IF EXISTS "anon_delete_emoji_library" ON emoji_library;
DROP POLICY IF EXISTS "anon_select_emoji_library" ON emoji_library;
CREATE POLICY "anon_select_emoji_library" ON emoji_library FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_emoji_categories" ON emoji_categories;
DROP POLICY IF EXISTS "anon_update_emoji_categories" ON emoji_categories;
DROP POLICY IF EXISTS "anon_delete_emoji_categories" ON emoji_categories;
DROP POLICY IF EXISTS "anon_select_emoji_categories" ON emoji_categories;
CREATE POLICY "anon_select_emoji_categories" ON emoji_categories FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_emoji_mappings" ON emoji_mappings;
DROP POLICY IF EXISTS "anon_update_emoji_mappings" ON emoji_mappings;
DROP POLICY IF EXISTS "anon_delete_emoji_mappings" ON emoji_mappings;
DROP POLICY IF EXISTS "anon_select_emoji_mappings" ON emoji_mappings;
CREATE POLICY "anon_select_emoji_mappings" ON emoji_mappings FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_emoji_state" ON emoji_state;
DROP POLICY IF EXISTS "anon_update_emoji_state" ON emoji_state;
DROP POLICY IF EXISTS "anon_delete_emoji_state" ON emoji_state;
DROP POLICY IF EXISTS "anon_select_emoji_state" ON emoji_state;
CREATE POLICY "anon_select_emoji_state" ON emoji_state FOR SELECT
    TO anon, authenticated USING (true);

DROP POLICY IF EXISTS "anon_insert_emoji_chat_overrides" ON emoji_chat_overrides;
DROP POLICY IF EXISTS "anon_update_emoji_chat_overrides" ON emoji_chat_overrides;
DROP POLICY IF EXISTS "anon_delete_emoji_chat_overrides" ON emoji_chat_overrides;
DROP POLICY IF EXISTS "anon_select_emoji_chat_overrides" ON emoji_chat_overrides;
CREATE POLICY "anon_select_emoji_chat_overrides" ON emoji_chat_overrides FOR SELECT
    TO anon, authenticated USING (true);

COMMENT ON TABLE emoji_library IS
    'The owner''s premium/custom emoji definitions (one row per Telegram custom-emoji document). UNIQUE (owner_id, document_id) is the deduplication identity; alt_text is Telegram''s own fallback text.';
COMMENT ON COLUMN emoji_library.source IS
    '''imported'' (the only producer today) or ''manual'' (documented, currently unproduced).';
COMMENT ON COLUMN emoji_library.source_msg_id IS
    'The Saved Messages message the entry was imported from; NULL for sticker-set members, which carry no message origin.';

COMMENT ON TABLE emoji_categories IS
    'The owner''s emoji categories. is_custom is the EXPLICIT Custom-Category flag (never inferred from the name); source_category_ids is the ordered source list of a composed Custom Category.';
COMMENT ON COLUMN emoji_categories.is_custom IS
    'TRUE for a Custom Category composed from other categories (Phase 5). Ordinary rows default to FALSE.';
COMMENT ON COLUMN emoji_categories.source_category_ids IS
    'Ordered id list of a Custom Category''s sources (snapshot-on-compose); [] for ordinary categories.';

COMMENT ON TABLE emoji_mappings IS
    'simple emoji → library entry, inside one category. References document_id only; premium emoji definitions are never duplicated. UNIQUE (owner_id, category_id, simple_emoji) makes a silent overwrite impossible.';
COMMENT ON COLUMN emoji_mappings.category_id IS
    'Application-level reference to emoji_categories.id (owner-scoped, validated by the service). Deliberately not a foreign key: dangling references fail closed in code.';

COMMENT ON TABLE emoji_state IS
    'ONE global replacement-state row per owner: the replacement toggle (default FALSE — the feature is OFF on first boot) and the global default category.';
COMMENT ON COLUMN emoji_state.global_default_category_id IS
    'The owner-approved (global-only) active category. Application-level reference; NULL means none.';

COMMENT ON TABLE emoji_chat_overrides IS
    'Per-chat category overrides: IMPLEMENTED state of a code path the owner did NOT approve (the approved Active Category scope is global-only). Kept additively; the approved resolution order never consults it.';
COMMENT ON COLUMN emoji_chat_overrides.override_category_id IS
    'The per-chat override category (application-level reference). NULL restores the global default at resolution time.';

NOTIFY pgrst, 'reload schema';

SELECT 'emoji tables' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('emoji_library', 'emoji_categories', 'emoji_mappings',
                     'emoji_state', 'emoji_chat_overrides');

SELECT 'emoji_categories.is_custom' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'emoji_categories'
  AND column_name IN ('is_custom', 'source_category_ids');
