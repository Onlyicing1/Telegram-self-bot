/*
# Panel settings: the Auto-Reaction columns

The Auto-Reaction setting (Emoji & Reaction, §32) is two owner-editable values on
the existing singleton `panel_settings` row:

- `auto_reaction_enabled` — whether the owner's own outgoing messages may be
                          reacted to automatically. FALSE by default: applying
                          this migration can never enable a reaction.
- `auto_reaction_emoji`   — the EXACT Telegram reaction emoji the owner picked in
                          the General panel. `''` (the empty string) is "none
                          selected", which is the application-level default.

`backend/services/settings_service.py` owns both keys (`_DEFAULTS`, validators,
typed accessors), `backend/services/panel_settings_repository.py` writes them as
real columns (`update_field`), and
`backend/services/auto_reaction_service.py::process_outgoing_message` consults
them for every outgoing message. No earlier migration created the two columns,
so on a database built from the repository's migrations the update is rejected
(the column does not exist), the setting survives only in the process cache, and
a restart/redeploy silently loses it. This migration is the missing half of the
already-documented column-per-setting contract (§6).

Additive and idempotent:

1. `ADD COLUMN IF NOT EXISTS` adds each column WITHOUT a default, so an existing
   column keeps every stored value untouched.
2. `SET DEFAULT` binds the final default (FALSE / '').
3. `UPDATE ... WHERE <column> IS NULL` backfills only rows that have no value —
   a valid stored value is never replaced.
4. `SET NOT NULL` applies the final contract, which the backfill just satisfied.

Nothing else is created, dropped or rewritten: no index, no policy, no grant, no
other column and no other row. The table already has RLS enabled with a
SELECT-only policy for `anon`/`authenticated`; all writes go through the backend
service-role key, so no security change is needed or made.

Applying this to the live Supabase project is a separate manual owner action
(Supabase SQL editor as `postgres`, or the one-paste setup in
DATABASE_ARCHITECTURE.md §31.3).

## MANUAL SUPABASE ACTION REQUIRED

Rollback (the Auto-Reaction setting then degrades to the in-memory cache, OFF on
every restart — exactly the state this migration fixes):

    ALTER TABLE panel_settings DROP COLUMN IF EXISTS auto_reaction_emoji;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS auto_reaction_enabled;
*/

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_reaction_enabled boolean;
ALTER TABLE panel_settings ALTER COLUMN auto_reaction_enabled SET DEFAULT false;
UPDATE panel_settings SET auto_reaction_enabled = false WHERE auto_reaction_enabled IS NULL;
ALTER TABLE panel_settings ALTER COLUMN auto_reaction_enabled SET NOT NULL;

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_reaction_emoji text;
ALTER TABLE panel_settings ALTER COLUMN auto_reaction_emoji SET DEFAULT '';
UPDATE panel_settings SET auto_reaction_emoji = '' WHERE auto_reaction_emoji IS NULL;
ALTER TABLE panel_settings ALTER COLUMN auto_reaction_emoji SET NOT NULL;

NOTIFY pgrst, 'reload schema';

SELECT 'panel_settings auto-reaction columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'panel_settings'
  AND column_name IN ('auto_reaction_enabled', 'auto_reaction_emoji');
