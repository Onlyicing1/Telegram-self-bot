/*
# Add the user-facing display name to saved_items

Save V2 — part 1 (data model + shared Save metadata).

`saved_items` had no owner-facing name. The only handle a saved item had was
its immutable `save_code` (`S####`), so every retrieval had to round-trip
through an opaque code, and the UI degraded to showing `media_type` as the
"name" (`retrieve_service._display_name`). `caption` cannot carry the name —
it is a rendered multi-line block that retrieval injects into the delivered
caption — and `file_name` is the SOURCE file's name from Telegram plus the
generated `photo_<code>.jpg` / `<code>.bin` fallback, which Save V2 must keep
distinct from an owner label.

`display_name` is a new, additive, nullable `text` column that carries the
owner's own name for the item, for example:

    University Weekly Schedule — Semester Two

Contract of the column:

- **nullable, no default.** Existing rows and every save without a custom name
  stay `NULL`. `NULL` means "the owner gave no name": display falls back to
  the source filename and then to `media_type`, and matching never treats
  `NULL` as a value. The column is never filled in by an LLM or by any
  automatic process — metadata the owner did not provide is never invented.
- **independent of `save_code`.** `save_code` stays the immutable internal
  identity; naming, renaming and deleting an item never touches it. No code
  path updates `save_code`.
- **bounded and normalized in the application layer.** Empty or
  whitespace-only names are stored as `NULL` (never `''`), and a name longer
  than the application bound is refused with an honest failure instead of
  being silently truncated (`save_service.normalize_display_name`).

This migration deliberately does NOT change `tags`. That column already exists
as `text[] DEFAULT '{}'`; Save V2 changes only its PRODUCER — from five
machine-generated hashtags (`#saved`, `#saved_photo`, `#saved_2026`, …) to the
owner's own semantic tags. That is an application-layer change, needs no DDL,
and is backward compatible at the storage level:

- **write path** — a save writes the owner's normalized tags, or `'{}'` when
  the owner gave none (`save_service.execute_save`). Synthetic hashtags are no
  longer written; they are still rendered in the caption, so the saved message
  looks the same as before.
- **existing rows are untouched** — no backfill, no rewrite, no truncation.
  Historical rows keep the hashtags they were saved with. They remain fully
  readable and retrievable by `save_code`; a resolver must simply ignore
  entries that start with `#` (a `#`-prefixed tag is not an owner tag).

The repository's canonical reconciliation script (§30 of
DATABASE_ARCHITECTURE.md) predates this column and is deliberately left
byte-frozen as the reconciliation snapshot. This file is its additive,
forward-only successor; together they are the current contract for
`saved_items` (see DATABASE_ARCHITECTURE.md §2 and §20).

## MANUAL SUPABASE ACTION REQUIRED

Apply (idempotent, non-destructive, no row is rewritten):

    ALTER TABLE saved_items
        ADD COLUMN IF NOT EXISTS display_name text;

Then read the verification query at the bottom of this file: zero rows returned
means the column exists.

**Order matters.** Apply this BEFORE the application starts writing names
(the AI `save this as X` surface and the manual Name & tags step arrive in a
later phase). Until the column exists, a save WITHOUT a name is unaffected —
`execute_save` omits the key entirely when the owner gave no name — and a save
WITH a name fails honestly at insert time rather than being silently dropped.
Apply it BEFORE the canonical script only if `saved_items` already exists;
otherwise run the canonical script first (it creates the table).

Rollback (the stored names are lost; every other column is untouched):

    ALTER TABLE saved_items
        DROP COLUMN IF EXISTS display_name;

## Security

No change: RLS is already enabled on `saved_items` with a SELECT-only policy
for `anon` + `authenticated`, and all writes go through the backend
service-role key. The new column inherits that posture, and it is covered by
the existing owner-scoped reads. Nothing secret may be placed in it — it is
display metadata, and it is rendered in panels and in the dashboard.

Idempotent: safe to run more than once.
*/

ALTER TABLE saved_items
    ADD COLUMN IF NOT EXISTS display_name text;

-- PostgREST caches the schema: without this the API keeps rejecting an INSERT
-- or UPDATE that names display_name until the cache expires on its own.
NOTIFY pgrst, 'reload schema';

-- Verification — zero rows means the database now carries the column.
SELECT v.tbl || '.' || v.col AS missing_canonical_column
FROM (VALUES ('saved_items','display_name')) AS v(tbl, col)
LEFT JOIN information_schema.columns c
    ON c.table_schema = 'public' AND c.table_name = v.tbl AND c.column_name = v.col
WHERE c.column_name IS NULL
ORDER BY 1;
