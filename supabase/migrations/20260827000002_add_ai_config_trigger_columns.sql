/*
# Add trigger_en / trigger_fa to ai_config

Resolves DATABASE_ARCHITECTURE.md §19.1 / §20 item 2.

`config_store._save_config_sync` (backend/ai/config_store.py) includes
`trigger_en` and `trigger_fa` in EVERY upsert payload (keys are always
present, empty string normalized to NULL). Before this migration the
unknown columns made PostgREST reject the whole insert/update, so the
entire AI configuration (provider, model, temperature, max_tokens,
system_prompt, history_budget, is_configured) silently degraded to the
in-memory fallback on every save and was lost on restart.

Columns are nullable text with NULL default, exactly as specified in §7:
empty means "not set"; validation (single word, at least one non-empty,
case rules) is enforced in application code (`validate_triggers`).

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

2. Security
- No change: RLS is already enabled on `ai_config` with a SELECT-only
  policy for anon + authenticated. All writes go through the backend
  service-role key (bypasses RLS).
*/

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS trigger_en text DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS trigger_fa text DEFAULT NULL;
