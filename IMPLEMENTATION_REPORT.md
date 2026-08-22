# Implementation Report — LifeOS Telegram Self-Bot

## Execution 17 — AI Usage Read-Side / Observability

### Task / Result

Implement the read-side consumption of persisted AI usage data
(`ai_usage` + `ai_provider_stats`) through the existing repository
abstractions, with honest token-source aggregation and safe degradation.
**Result: IMPLEMENTED** — the Usage panel now consumes persisted data
(additively) via a new async reader; live Supabase reads follow the same
safe-degradation contract as the rest of the persistence layer.

### Files actually changed

- `backend/ai/database/usage_reader.py` — **NEW**. Async read accessor over
  `RepositoryManager` (never Supabase directly):
  - `total_tokens(owner_id)`, `daily_tokens(owner_id, date)`,
    `recent(owner_id, limit)`, `provider_stats(owner_id)`.
  - `summary(owner_id, *, since, limit)` → `UsageSummary` with a
    `TokenSourceBreakdown` (actual / estimated / unavailable) and a
    `sources` tuple that preserves every label actually seen — even at
    zero counts — so "unavailable" is never lost from aggregation.
  - Every call runs off the event loop (`asyncio.to_thread`) under a
    bounded 3s timeout; `CancelledError` re-raised; any other failure
    logs and returns the safe default (`0` / `[]` / all-zero summary
    with `available=False`).
- `backend/bot/handlers/ai.py` — `_ai_usage_panel_handler` now appends a
  persisted-usage line (`Saved · N requests · X tokens · <source labels>`)
  when saved data exists for the same window; new helpers
  `_read_persisted_usage` (window mapping + safe fallback) and
  `_persisted_usage_line` (honest source suffix; "tokens unavailable"
  instead of a fabricated zero). Persisted reads are strictly additive —
  empty or failed reads leave the panel byte-identical to before.
- `tests/test_40_usage_read_side.py` — **NEW**, 15 tests.
- `IMPLEMENTATION_REPORT.md` — replaced with only this report.

### Exact behavior changed

- Persisted usage is now consumable by the application: total usage,
  daily usage, recent records, and per-provider statistics read through
  `RepositoryManager` with the same failure-tolerant semantics as the
  write path.
- Token honesty preserved end-to-end: aggregation never merges
  actual/estimated/unavailable into a single unlabeled number; the
  breakdown is exposed to callers and rendered compactly in the panel
  (`actual`, `≈`, `unavailable`). Unknown source labels are counted as
  unlabeled — never as actual.
- The Usage panel shows the session view (RAM telemetry, unchanged and
  pinned by existing tests) plus a persisted line when saved data exists.
  Range windows mirror the existing today / 7d / 30d semantics.
- Repository/DB read failures degrade to safe defaults and never block
  the event loop, raise, or affect AI execution or the panel.

### Intentionally untouched

Provider retry/fallback policy · provider implementations · memory
implementation · telemetry contract · Save system · Ghost Room · Telegram
core handlers unrelated to AI usage · frontend · runtime supervisor/
watchdog · database schema · migrations · DATABASE_ARCHITECTURE.md
(no schema change was needed this chunk).

### Database impact

None. No SQL, no migrations, no schema changes. Reads target the
already-verified `ai_usage` / `ai_provider_stats` tables through the
existing Supabase repositories; without Supabase the in-memory fallbacks
serve the same interface.

### Tests actually run and exact results

- `tests/test_40_usage_read_side.py` — **15 passed** (total read, daily
  read, recent read, provider-stats read, actual/estimated/unavailable
  preservation, mixed-source non-merging, window filter, failure
  degradation, no-direct-Supabase guard, panel persisted line, panel
  unchanged when empty, panel survives read failure, line formatting).
- Full suite — **678 passed, 0 failed, 1 warning** (baseline 663 + 15;
  the warning is the pre-existing multipart deprecation).
- `python3 -m compileall -q backend` — PASS.
- `git diff --check` — PASS.

### Remaining limitations

- Persisted provider stats are all-time aggregates (the
  `ai_provider_stats` table has no timestamp window); windowed
  success/failure counts are not derivable from persisted rows because
  the `ai_usage` schema stores no status column — the session (RAM)
  block continues to provide failures/fallbacks for the window.
- Live Supabase reads are covered by the same code path as in-memory;
  end-to-end verification against live data remains the owner's manual
  step.

### Commit / push / remote verification

- **Commit:** (filled at delivery)
- **Push:** pushed to `origin/main`; `git fetch origin` → local HEAD ==
  origin/main.
- **Final git status:** working tree clean.
