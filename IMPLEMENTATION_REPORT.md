# Implementation Report — LifeOS Telegram Self-Bot

## Execution 16 — AI Usage Persistence (`ai_usage` + `ai_provider_stats`)

### Task / Result

Wire the existing normalized AI execution telemetry into the documented
`ai_usage` and `ai_provider_stats` persistence layer, exactly once per
request, with deterministic token honesty. **Result: IMPLEMENTED
(code-wired) — live persistence NOT YET PROVEN** until the documented
migrations are applied manually by the owner (see Database impact).

### Files actually changed

- `backend/ai/database/usage_repository.py` — added `SupabaseUsageRepository`
  (create / total_tokens / daily_tokens / recent, all sync, all degrade
  safely on failure or missing Supabase).
- `backend/ai/database/provider_stats_repository.py` — added
  `SupabaseProviderStatsRepository` (get_or_create / record_request
  read-modify-write upsert on `(provider_name, owner_id)` / get / list_all).
- `backend/ai/database/usage_recorder.py` — **NEW**. Single recording choke
  point consuming the normalized `AIExecutionRecord` returned by
  `telemetry.record_execution`: writes one `ai_usage` row + one
  `ai_provider_stats` aggregate update, off the event loop via
  `asyncio.to_thread` with a bounded 5s timeout. Never raises.
- `backend/ai/database/manager.py` — wires the Supabase repositories into
  `RepositoryManager` when Supabase is available (in-memory fallback
  otherwise).
- `backend/ai/engine/dispatcher.py` — all three telemetry recording sites
  (provider dispatch result, deterministic fast path, `_fail` path) now
  capture the returned record and schedule `record_usage(...)` through
  `guarded_create_task`; skipped when no event loop is running (direct sync
  test callers) to avoid coroutine leaks.
- `DATABASE_ARCHITECTURE.md` — doc-first updates: `ai_usage.token_source`
  column added to §13 schema, §12/§13 wiring notes updated, §19.8 resolution
  updated, migration-generation items referenced (no SQL executed).
- `tests/test_39_usage_persistence.py` — **NEW**, 5 tests.
- `IMPLEMENTATION_REPORT.md` — replaced with only this report (per task
  instruction; prior history remains in git).

### Exact behavior changed

- Every AI execution that already produced a telemetry record now also
  produces exactly one persisted usage row (`ai_usage`) and one
  provider-stats upsert (`ai_provider_stats`) — scheduled once from the
  dispatcher after `record_execution` returns, so retries/fallbacks inside
  the provider loop cannot multiply persistence.
- Token honesty preserved verbatim: the row carries the record's
  `token_source` (`actual` / `estimated` / `unavailable`) and the exact
  provider-reported input/output/total counts. Unavailable usage persists
  as the true zeros + `token_source=unavailable`, never fabricated numbers.
- Persistence failures are logged and reported as `False` — they never
  raise, never affect the AI response, never touch the telemetry record.
- No background task loops introduced; each execution schedules exactly one
  bounded recorder task.

### Intentionally untouched

Memory implementation · provider retry/fallback policy · provider
implementations · AI telemetry contract · AI UI · settings · Save system ·
Ghost Room · frontend · runtime supervisor/watchdog · migrations (none
created). No new architecture — reuses the existing telemetry record,
`RepositoryManager`, `task_guard.guarded_create_task`, and sync-repository
convention.

### Database / schema impact

- **No SQL executed, no migration created, no Supabase modified.**
- Required schema (`ai_usage`, `ai_provider_stats`) is documented in
  DATABASE_ARCHITECTURE.md §12/§13; **the owner must apply the migration
  manually**. Until then, the runtime degrades gracefully: Supabase writes
  fail with logged warnings and behavior falls back to the existing
  in-memory repositories.

### Validation (all actually run)

- `tests/test_39_usage_persistence.py` — **5 passed** (success persistence,
  input/output tokens, actual/estimated/unavailable semantics, retry/fallback
  accounting, failure-does-not-break-execution, no-duplicate-per-execution,
  provider/model metadata).
- Full suite — **663 passed, 0 failed, 1 warning** (baseline 658 + 5; the
  warning is the pre-existing async deprecation).
- `python3 -m compileall -q backend` — PASS.
- `git diff --check` — PASS.
- `git status` — only the intended files changed (5 modified + 2 new + report).

### Remaining limitations / work

- Live Supabase persistence is **NOT YET PROVEN** until the owner applies
  the §12/§13 migration manually.
- Read-side consumption of persisted usage (dashboards/panels beyond the
  in-memory repositories) is out of scope for this chunk.
- `estimated_cost_usd` is persisted as `0.0` (no cost model exists; not
  invented here).

### Commit / push / remote verification

- **Commit:** `8480a48`
- **Push:** pushed to `origin/main`; `git fetch origin` → local HEAD ==
  origin/main.
- **Working tree:** clean.

### Stop

Chunk complete. The following chunk (per contract) is not started.
