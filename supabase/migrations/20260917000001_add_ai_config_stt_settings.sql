/*
# Add the Gemini STT settings to ai_config

Durable storage for the three owner-editable speech-to-text behavior settings
(AI -> Settings -> Advanced):

- `stt_model`    — the dedicated transcription model, or NULL for the general
                   media model (the engine default).
- `stt_language` — an explicit BCP-47 language hint, or NULL for automatic
                   detection.
- `stt_passes`   — the bounded number of recognition passes (1..3, default 1,
                   i.e. the existing single-pass route).

`backend/ai/config_store.py` persists all three keys in every `ai_config`
upsert payload and merges them in `get_config` with the defaults above, so
before this column set exists the whole upsert degrades to the in-memory
fallback exactly as documented for the trigger columns
(see DATABASE_ARCHITECTURE.md §19.1) — the settings are then lost on restart.

The columns are additive and non-breaking: every existing read path keeps its
behavior through the `get_config` defaults merge (empty model, empty language,
one pass = the current behavior exactly).

This is NOT a second configuration store: the values live on the existing
per-owner `ai_config` row, which is the only place the AI Settings surface
persists anything.

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

## MANUAL SUPABASE ACTION REQUIRED

Apply (idempotent):

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS stt_model text;

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS stt_language text;

    ALTER TABLE ai_config
        ADD COLUMN IF NOT EXISTS stt_passes integer NOT NULL DEFAULT 1;

    ALTER TABLE ai_config
        DROP CONSTRAINT IF EXISTS ai_config_stt_passes_range;

    ALTER TABLE ai_config
        ADD CONSTRAINT ai_config_stt_passes_range
        CHECK (stt_passes BETWEEN 1 AND 3);

Rollback (after any rollback, `get_config` still serves the defaults — empty
model, empty language, one pass — so the application keeps working; the stored
values are lost):

    ALTER TABLE ai_config
        DROP CONSTRAINT IF EXISTS ai_config_stt_passes_range;

    ALTER TABLE ai_config
        DROP COLUMN IF EXISTS stt_passes;

    ALTER TABLE ai_config
        DROP COLUMN IF EXISTS stt_language;

    ALTER TABLE ai_config
        DROP COLUMN IF EXISTS stt_model;

2. Security
- No change: RLS is already enabled on `ai_config` with a SELECT-only
  policy for anon + authenticated. All writes go through the backend
  service-role key (bypasses RLS).
*/

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_model text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_language text;

ALTER TABLE ai_config
    ADD COLUMN IF NOT EXISTS stt_passes integer NOT NULL DEFAULT 1;

ALTER TABLE ai_config
    DROP CONSTRAINT IF EXISTS ai_config_stt_passes_range;

ALTER TABLE ai_config
    ADD CONSTRAINT ai_config_stt_passes_range
    CHECK (stt_passes BETWEEN 1 AND 3);
