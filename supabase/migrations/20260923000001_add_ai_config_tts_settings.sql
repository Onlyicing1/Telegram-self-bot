/*
# Add the Text-to-Speech control-plane settings to ai_config

Durable storage for the three owner-editable speech-synthesis settings
(AI -> Media Analysis -> Text-to-Speech): the REGISTERED provider/model/voice
triple the owner picked.

- `tts_provider` — the registered provider id (`openai`), or NULL for the default
                   provider.
- `tts_model`    — a model id that provider offers, or NULL for that provider's
                   default model.
- `tts_voice`    — a voice id that model offers, or NULL for that model's default
                   voice.

`backend/ai/config_store.py` persists all three keys in every `ai_config` upsert
payload and merges them in `get_config` with the defaults above, so before this
column set exists the whole upsert degrades to the in-memory fallback exactly as
documented for the trigger columns and the STT settings columns (see §19.1) — the
settings are then lost on restart.

The columns are additive and non-breaking: every existing read path keeps its
behavior through the `get_config` defaults merge (an empty triple is the DEFAULT
SELECTION — the default provider, its default model, that model's default voice —
which is exactly the pre-existing single-provider behavior).

This is NOT a second configuration store: the values live on the existing
per-owner `ai_config` row, which is the only place the AI surfaces persist
anything. API keys never live here — provider credentials belong to the existing
credential store documented in §29, which keeps them in Supabase Vault and stores
no secret in any ordinary column.

No CHECK constraint is added on purpose: these columns hold REGISTRY TOKENS, and
the registry (`backend/ai/tts_control_plane.py`) is the single authority on which
tokens are valid — a SQL CHECK would duplicate that allowlist and drift from it.
An unusable stored combination is degraded deterministically by the control plane
to a valid one and reported, so a stale value can never reach a provider.

Idempotent: safe to run more than once. Applying it to the live Supabase project
is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply (idempotent):

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS tts_provider text;

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS tts_model text;

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS tts_voice text;

Rollback (after any rollback `get_config` still serves the defaults — the default
selection — so the application keeps working; the stored values are lost):

    ALTER TABLE ai_config DROP COLUMN IF EXISTS tts_voice;
    ALTER TABLE ai_config DROP COLUMN IF EXISTS tts_model;
    ALTER TABLE ai_config DROP COLUMN IF EXISTS tts_provider;

2. Security
- No change: RLS is already enabled on `ai_config` with a SELECT-only policy for
  anon + authenticated. All writes go through the backend service-role key
  (bypasses RLS).
*/

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS tts_provider text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS tts_model text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS tts_voice text;

NOTIFY pgrst, 'reload schema';

SELECT 'ai_config tts settings' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'ai_config'
  AND column_name IN ('tts_provider', 'tts_model', 'tts_voice');
