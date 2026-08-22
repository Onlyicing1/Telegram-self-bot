# Implementation Report — LifeOS Telegram Self-Bot

## Execution 15 — 2026-08-22

**Task:** TOKEN ACCOUNTING RETRY FIXES (CHUNK 15)

### Task / result

Fixed the two confirmed token-accounting divergences in the AI dispatcher:
provider usage from responses superseded by (1) the empty-response retry and
(2) the action-recovery retry was being discarded before accumulation. Every
provider attempt that actually occurred now contributes its available usage
exactly once. **Status: FIXED for the two identified paths** (proven by
regression tests).

### Files changed

- `backend/ai/engine/dispatcher.py` — the only production file changed.
- `tests/test_38_token_accounting.py` — new regression suite (6 tests).
- `IMPLEMENTATION_REPORT.md` — replaced with this report (per instruction; the
  prior report history remains in git history at commits `b27215d`/`75c5009`).

### Exact behavior changed

1. Added module-level `_accumulate_usage(target, source)` — sums a provider
   response's reported usage into an accumulator; missing/zero fields
   contribute nothing (no invented counts).
2. Added `discarded_usage` accumulator in `Dispatcher.dispatch` before the
   provider loop.
3. **Empty-response retry**: immediately before `response = retry_response`,
   the superseded empty attempt's usage is accumulated.
4. **Action-recovery retry**: immediately before each of the three replacement
   sites (`response = recovery_response`, and both `response = candidate`
   branches), the superseded response's usage is accumulated; additionally the
   selected recovery response now ORs into `provider_usage_reported` so the
   `token_source` label matches the retained totals.
5. The final `usage` dict now merges `discarded_usage` into the final
   response's usage; continuation rounds still stack on top. Each response is
   counted exactly once — discarded attempts via the accumulator, the final
   response via its own usage dict.

No retry policy, provider selection, fallback behavior, memory behavior,
telemetry contract, or normal single-attempt accounting was changed.

### Intentionally untouched

- Memory implementation (wiring + bounds from Execution 14).
- Provider retry policy and provider implementations (bug lived entirely in
  the dispatcher).
- Supabase, SQL, migrations, database schema.
- Ghost Room, AI UI/settings, frontend, Telegram handlers.
- No new architecture; reuses the existing usage dict + `token_source`
  normalization.

### Database/schema impact

None. No SQL executed, no migrations, no schema changes.

### Validation (all actually run)

- `tests/test_38_token_accounting.py` — **6 passed** (empty-retry retention,
  recovery-retry retention, recovery-candidate retention, normal single-attempt
  unchanged, unavailable stays unavailable, no double-counting).
- Full suite: **658 passed, 0 failed, 1 warning** (baseline 652 + 6 new; the
  warning is the pre-existing starlette `python_multipart`
  PendingDeprecationWarning).
- `python3 -m compileall -q backend` — OK.
- `git diff --check` — OK.
- `git status` — only `dispatcher.py` modified plus the new test file before
  report replacement.

### Remaining limitations

- A failed, non-selected retry attempt that reports usage is still not added
  to totals (pre-existing semantics; its `provider_usage_reported` OR in the
  empty-retry block is unchanged). This is outside the two named bugs and only
  affects an already-failed request.

### Commit / push / remote verification

- Commit message: `fix: retain usage from superseded retry attempts`
- Pushed to `origin/main`; `git fetch origin` verified local HEAD ==
  origin/main at the exact commit hash.
- Final working-tree state: clean.
