/*
# Create ai_provider_stats table

Resolves DATABASE_ARCHITECTURE.md §19.8 / §20 item 4; schema is generated
from §12 and verified against the only live writer,
`SupabaseProviderStatsRepository.record_request`
(backend/ai/database/provider_stats_repository.py), which upserts the full
`ProviderStatsRecord.as_dict()` payload with
`on_conflict="provider_name,owner_id"`.

- The composite PRIMARY KEY `(provider_name, owner_id)` — named
  `ai_provider_stats_pkey`, exactly as §12 specifies — IS the unique
  constraint the upsert conflict target resolves against. One row per
  (provider, owner) pair.
- `last_request_at` is nullable (NULL until the first recorded request).
- Rows are written by `usage_recorder.record_usage` (read-modify-write
  upsert per provider/owner, asyncio.to_thread, bounded timeout; failures
  are logged and never break AI execution).

Idempotent: safe to run more than once. Applying it to the live Supabase
project is a separate manual owner action.

2. Security
- RLS enabled; SELECT-only granted to anon + authenticated (read-only
  dashboard access). All writes go through the backend service-role key
  (bypasses RLS).
*/

CREATE TABLE IF NOT EXISTS ai_provider_stats (
    provider_name            text        NOT NULL,
    owner_id                 bigint      NOT NULL DEFAULT 0,
    total_requests           integer     NOT NULL DEFAULT 0,
    successful_requests      integer     NOT NULL DEFAULT 0,
    failed_requests          integer     NOT NULL DEFAULT 0,
    total_prompt_tokens      integer     NOT NULL DEFAULT 0,
    total_completion_tokens  integer     NOT NULL DEFAULT 0,
    avg_latency_ms           real        NOT NULL DEFAULT 0,
    last_request_at          timestamptz,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_name, owner_id)
);

ALTER TABLE ai_provider_stats ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon_select_ai_provider_stats" ON ai_provider_stats;
CREATE POLICY "anon_select_ai_provider_stats" ON ai_provider_stats FOR SELECT
    TO anon, authenticated USING (true);
