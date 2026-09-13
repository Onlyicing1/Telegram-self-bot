/*
# Add show_question to ai_config

Durable storage for the presentation-only "Show my message in AI replies"
preference (AI Settings). `backend/ai/config_store.py` persists this key in
every `ai_config` upsert payload (always present, boolean) and merges it in
`get_config` with a `False` default, so before this column exists the whole
upsert degrades to the in-memory fallback exactly as documented for the
trigger columns — the preference is then lost on restart.

The column is additive and non-breaking: every existing read path keeps its
value via the `get_config` defaults merge (`False` = current behavior, the
question block is hidden).

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply (idempotent):

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS show_question boolean NOT NULL DEFAULT false;

Rollback (after any rollback, `get_config` still serves the default `False`,
so the application keeps working; the stored value is lost):

    ALTER TABLE ai_config
        DROP COLUMN IF EXISTS show_question;

2. Security
- No change: RLS is already enabled on `ai_config` with a SELECT-only
  policy for anon + authenticated. All writes go through the backend
  service-role key (bypasses RLS).
*/

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS show_question boolean NOT NULL DEFAULT false;
