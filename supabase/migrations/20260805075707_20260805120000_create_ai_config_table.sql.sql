/*
# Create AI Configuration Persistence Table

1. New Tables
- `ai_config` — Singleton-per-owner AI configuration. Stores the user's selected
  provider, model, temperature, max_tokens, and other AI settings so they are
  restored automatically after restart. No more manual re-configuration.
  Columns:
  - `id` (bigserial, PK)
  - `owner_id` (bigint, unique — one config row per owner)
  - `provider` (text — e.g. 'openrouter', 'groq', 'gemini')
  - `model` (text — e.g. 'meta-llama/llama-3.3-70b-instruct')
  - `temperature` (real — 0.0 to 2.0, default 1.0)
  - `max_tokens` (integer — default 4096)
  - `system_prompt` (text — custom system prompt, empty = use default)
  - `history_budget` (integer — conversation history token budget, default 4000)
  - `is_configured` (boolean — whether the user has completed the setup wizard)
  - `last_request_at` (timestamptz — timestamp of last successful AI request)
  - `last_latency_ms` (real — latency of last successful request in ms)
  - `created_at` (timestamptz)
  - `updated_at` (timestamptz)

2. Security
- RLS enabled on `ai_config`.
- SELECT-only granted to anon + authenticated (read-only dashboard access).
- All writes go through the backend's service-role key which bypasses RLS.

3. Important Notes
- This table is a singleton per owner (owner_id is UNIQUE).
- Idempotent: uses IF NOT EXISTS so re-running is safe.
- Matches the existing schema pattern (no foreign keys, owner_id is a plain bigint).
*/

CREATE TABLE IF NOT EXISTS ai_config (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    temperature    real         DEFAULT 1.0,
    max_tokens      integer      DEFAULT 4096,
    system_prompt   text         DEFAULT '',
    history_budget  integer      DEFAULT 4000,
    is_configured   boolean      DEFAULT false,
    last_request_at timestamptz,
    last_latency_ms real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

ALTER TABLE ai_config
    DROP CONSTRAINT IF EXISTS ai_config_owner_id_key;
ALTER TABLE ai_config
    ADD CONSTRAINT ai_config_owner_id_key UNIQUE (owner_id);

CREATE INDEX IF NOT EXISTS idx_ai_config_owner ON ai_config (owner_id);

ALTER TABLE ai_config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_config" ON ai_config;
CREATE POLICY "anon_select_ai_config" ON ai_config FOR SELECT
    TO anon, authenticated USING (true);
