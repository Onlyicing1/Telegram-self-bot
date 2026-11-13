/*
# Panel settings: the fixed Intelligent Reaction probability
Intelligent Reaction is the AI-driven reaction mode for the owner's own outgoing
Telegram messages. The earlier migration
(`20261112140000_add_panel_settings_intelligent_reaction.sql`) already owns
`intelligent_reaction_enabled`; this migration adds the SECOND and final column
the same feature needs:

- `intelligent_reaction_probability` — an integer percentage from 0 to 100
                                       (inclusive), 25 by default. The owner
                                       configures it in the General panel, and
                                       the backend reads it for EVERY eligible
                                       outgoing message, so a saved change
                                       applies to the next message without a
                                       restart. 0% never reacts, 100% always
                                       reacts, and the AI never decides or
                                       overrides this value.

This migration is probability-only on purpose. It does not re-add, reset or
rewrite `intelligent_reaction_enabled`, so every stored enabled value is
preserved exactly as the owner left it.

Additive and idempotent:
1. `ADD COLUMN IF NOT EXISTS` adds the column WITHOUT a default, so an existing
   column keeps every stored value untouched.
2. `SET DEFAULT` binds the final default (25).
3. `UPDATE ... WHERE <column> IS NULL` backfills only rows that have no value.
   A stored percentage is never replaced.
4. `SET NOT NULL` applies the final contract, which the backfill just satisfied.
5. The CHECK constraint is (re)created only after the backfill, so it can never
   reject the normalization step or a valid stored value.

Nothing else is created, dropped or rewritten: no index, no policy, no grant, no
other column and no other row. The table already has RLS enabled with a
SELECT-only policy for `anon`/`authenticated`; all writes go through the backend
service-role key, so no security change is needed or made.

`backend/services/settings_service.py` owns the key (`_DEFAULTS`, validator,
typed accessors) and `backend/services/intelligent_reaction_service.py` applies
it per message. Applying this to the live Supabase project is a separate manual
owner action (Supabase SQL editor as `postgres`, or the one-paste setup in
DATABASE_ARCHITECTURE.md §31.3).

## MANUAL SUPABASE ACTION REQUIRED
Rollback (DESTRUCTIVE — the owner's stored percentage is destroyed and the
setting falls back to the in-memory default of 25 on the next load):
    ALTER TABLE panel_settings
        DROP CONSTRAINT IF EXISTS panel_settings_intelligent_reaction_probability_check;
    ALTER TABLE panel_settings DROP COLUMN IF EXISTS intelligent_reaction_probability;
*/
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS intelligent_reaction_probability integer;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_probability SET DEFAULT 25;
UPDATE panel_settings SET intelligent_reaction_probability = 25 WHERE intelligent_reaction_probability IS NULL;
ALTER TABLE panel_settings ALTER COLUMN intelligent_reaction_probability SET NOT NULL;
ALTER TABLE panel_settings DROP CONSTRAINT IF EXISTS panel_settings_intelligent_reaction_probability_check;
ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_intelligent_reaction_probability_check
    CHECK (intelligent_reaction_probability >= 0 AND intelligent_reaction_probability <= 100);
