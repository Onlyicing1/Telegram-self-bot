/*
# Create ai_usage table

Resolves DATABASE_ARCHITECTURE.md §19.8 / §20 item 5; schema is generated
from §13 and verified against the only live writer,
`SupabaseUsageRepository.create` (backend/ai/database/usage_repository.py),
whose exact insert payload is:

    owner_id, session_id, provider, model, prompt_tokens,
    completion_tokens, total_tokens, latency_ms, token_source, created_at

- `id` is NOT sent by the Supabase writer (the uuid4 on `UsageRecord`
  exists only on the in-memory path) — `bigserial` PRIMARY KEY matches
  the other AI tables and `count()` reads `.select("id", count="exact")`.
- `token_source` carries the token honesty label verbatim
  (`actual` / `estimated` / `unavailable`) — never fabricated.
- Rows are written by `usage_recorder.record_usage` after every AI
  execution via `RepositoryManager.usage` (asyncio.to_thread, bounded
  timeout; failures are logged and never break AI execution).

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

2. Security
- RLS enabled; SELECT-only granted to anon + authenticated (read-only
  dashboard access). All writes go through the backend service-role key
  (bypasses RLS).
*/

CREATE TABLE IF NOT EXISTS ai_usage (
    id                bigserial    PRIMARY KEY,
    owner_id          bigint       NOT NULL,
    session_id        text,
    provider          text,
    model             text,
    prompt_tokens     integer      DEFAULT 0,
    completion_tokens integer      DEFAULT 0,
    total_tokens      integer      DEFAULT 0,
    latency_ms        real         DEFAULT 0,
    token_source      text         DEFAULT NULL,
    created_at        timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_owner ON ai_usage (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage (created_at);

ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_usage" ON ai_usage;
CREATE POLICY "anon_select_ai_usage" ON ai_usage FOR SELECT
    TO anon, authenticated USING (true);
