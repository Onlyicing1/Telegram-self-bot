/*
# Add the Save V2 resolver's search indexes to saved_items

Save V2 — part 3 (deterministic retrieval resolution, 0 / 1 / N).

`retrieve_service.resolve_saved_items` resolves a saved-item request against
the owner's persisted metadata — an owner-scoped query that prefilters with
`display_name ILIKE '%token%'` (every token), whole-tag containment
(`tags.cs.{term}`), and orders by `created_at DESC, save_code ASC` with an
explicit LIMIT. Without indexes those prefilters are sequential scans over
`saved_items`; with them the owner filter + ordering is served by the
existing `idx_saved_items_owner_created` btree and the two match predicates
below are served by GIN.

- `idx_saved_items_display_name_trgm` — GIN trigram on `display_name`,
  serving the `display_name ILIKE '%token%'` prefilter. Requires `pg_trgm`,
  which the canonical reconciliation script (§30 of
  DATABASE_ARCHITECTURE.md) already enables; the
  `CREATE EXTENSION IF NOT EXISTS` below makes this file self-contained.
- `idx_saved_items_tags` — GIN on the `text[]` `tags` column, serving the
  whole-tag containment (`tags.cs.{...}`) prefilter.

Additive and idempotent: no column, no row, and no `save_code` is touched;
no historical migration or the byte-frozen canonical snapshot is modified.
This file is the successor of `20260921000001_add_saved_items_display_name.sql`
and follows the same additive-successor shape (§20 / §30.11 of
DATABASE_ARCHITECTURE.md).

## MANUAL SUPABASE ACTION REQUIRED

Run this whole file in the Supabase SQL Editor (as `postgres`). Then verify:
zero rows from the verification query at the bottom means both indexes exist.

Rollback (safe: indexes only, no data involved):

    DROP INDEX IF EXISTS idx_saved_items_display_name_trgm;
    DROP INDEX IF EXISTS idx_saved_items_tags;
*/

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_saved_items_display_name_trgm
    ON saved_items USING gin (display_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_saved_items_tags
    ON saved_items USING gin (tags);

-- PostgREST caches the schema: without this, newly created indexes may not
-- be visible to the query planner through the API until the cache expires.
NOTIFY pgrst, 'reload schema';

-- Verification — zero rows means the database now carries both indexes.
SELECT v.idx AS missing_save_v2_index
FROM (VALUES ('idx_saved_items_display_name_trgm'), ('idx_saved_items_tags')) AS v(idx)
LEFT JOIN pg_indexes i
    ON i.schemaname = 'public' AND i.indexname = v.idx
WHERE i.indexname IS NULL
ORDER BY 1;
