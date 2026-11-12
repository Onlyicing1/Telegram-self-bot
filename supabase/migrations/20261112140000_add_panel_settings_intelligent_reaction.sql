/*
# Panel settings: the Intelligent Reaction column

Intelligent Reaction is the AI-driven reaction mode for the owner's own outgoing
Telegram messages. It is one owner-editable value on the existing singleton
`panel_settings` row:

- `intelligent_reaction_enabled` — whether the AI may evaluate each owner-authored
                                  outgoing message and, after its own independent
                                  per-message probability check, apply the
                                  reaction it selected. FALSE by default:
                                  applying this migration can never enable it.

The setting is deliberately SEPARATE from the fixed Auto-Reaction setting
(`auto_reaction_enabled` / `auto_reaction_emoji`, part 14): the two modes are
mutually exclusive, and Intelligent Reaction never reads the fixed emoji.
`backend/services/settings_service.py` owns the key (`_DEFAULTS`, validator,
typed accessors) and enforces the exclusion, and
`backend/services/intelligent_reaction_service.py` consults it for every
outgoing message.

Additive and idempotent:

1. `ADD COLUMN IF NOT EXISTS` adds the column WITHOUT a default, so an existing
   column keeps every stored value untouched.
2. `SET DEFAULT` binds the final default (FALSE — OFF).
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

Rollback (Intelligent Reaction then degrades to the in-memory cache, OFF on every
restart — exactly the state this migration fixes):

    ALTER TABLE panel_settings DROP COLUMN IF EXISTS intelligent_reaction_enabled;
*/

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS intelligent_reaction_enabled boolean;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_enabled SET DEFAULT false;
UPDATE panel_settings SET intelligent_reaction_enabled = false WHERE intelligent_reaction_enabled IS NULL;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_enabled SET NOT NULL;

NOTIFY pgrst, 'reload schema';

SELECT 'panel_settings intelligent-reaction column' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'panel_settings'
  AND column_name  = 'intelligent_reaction_enabled';
