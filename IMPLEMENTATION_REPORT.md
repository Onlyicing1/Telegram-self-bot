# Implementation Report — Ghost Seen v2

## Stage 7 — AI Reply execution hardening

### Summary

Production Stage 6 behavior remains unchanged for the owner: `AI Reply → Context 1/5/10/20 → Disclosure Yes/No → automatic generation → automatic delivery`. Stage 7 adds bounded execution and race protection only; no prompt UX, provider/model UI, confirmation, Send button, or Stage 8 behavior was added.

### Files changed

- `backend/bot/handlers/ghost_seen_v2.py` — bounded AI execution timeout, per-panel operation lock, strict result-size validation, final privacy/selection validation, and cleanup on every terminal path.
- `tests/test_62_ghost_seen_v2_stage7.py` — focused timeout, exception, invalid-result, success, duplicate-concurrency, privacy-race, and UI regression tests.
- `IMPLEMENTATION_REPORT.md` — this latest report.

### Hardening decisions

- Engine execution is bounded by `_AI_TIMEOUT_S = 45.0` using `asyncio.wait_for`; timeout cancellation prevents a late result from reaching delivery.
- Each panel chat has one server-side `asyncio.Lock`; duplicate or concurrent callbacks fail closed and cannot produce a second delivery.
- Results must be successful, non-empty after stripping, and no larger than Telegram's 4096-character text limit. Meaningful output is never silently truncated.
- The existing Engine → Dispatcher → ProviderManager/provider architecture is reused. Provider failure and Engine exceptions are honest failures with zero delivery.
- Before delivery, source permission and the exact one-message selection are rechecked. Delivery uses the validated numeric source chat and selected Telegram message ID as reply target; panel chat ID is never the destination.
- AI transient state and selection are cleaned after success, generation failure, timeout, invalid result, delivery failure, duplicate rejection, and permission/selection invalidation. External cancellation is re-raised after cleanup.

### Tests and validation

- Stage 7 focused tests: **7 passed**
- All Ghost Seen v2 tests: **158 passed**
- Full Python suite: **964 passed, 23 skipped, 1 warning**
- `python -m compileall -q backend`: PASS
- `git diff --check`: PASS
- `bun tsc -b --noEmit`: PASS

The initial brace/glob test invocation was rejected by pytest as an invalid literal path; the corrected explicit file-list invocation passed all Ghost Seen v2 tests.

### Scope verification

- Exactly one `INVESTIGATION.md` exists.
- No database schema, SQL, migration, or deployment changes.
- You.com, web search, provider infrastructure, Save, Delete, Retrieve, Profile, Render configuration, and unrelated handlers were untouched.
- Telegram live E2E was **not performed**; tests use mocks and no live provider/account action.

### Git status

Git delivery is pending final commit and remote verification.
