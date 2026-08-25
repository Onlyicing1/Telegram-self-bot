# Implementation Report — Ghost Seen v2

## AI tool isolation and main Search action repair

### Root causes

- Ghost Seen AI Reply constructed a normal `AIRequest`, while `Dispatcher.dispatch()` unconditionally exposed tool schemas, ran the deterministic local fast path, parsed structured actions, and executed provider-emitted tool calls. The existing general AI path therefore had no request-specific capability boundary.
- Main Browser Search rendered valid `action:ghost_seen_v2_open:<source_chat_id>` callbacks, but its direct input-handler edit did not persist the active Browser query in the lifecycle session. The open action also used the raw callback event message ID instead of the lifecycle-resolved panel message ID for inline callbacks. The registered action was present; the failure was session/state continuity, not a missing or duplicate action.

### Implementation

- Added immutable `AIRequest.allow_tools`, defaulting to `True` so normal owner AI requests retain existing tool access.
- Ghost Seen AI Reply now passes `allow_tools=False` through the existing Engine -> Dispatcher -> ProviderManager path.
- Dispatcher tool schemas, deterministic fast path, structured-action/recovery handling, and ToolExecutor execution are gated by the request capability. Provider tool calls that arrive despite a disabled request are converted to an honest failed result and never reach a tool executor.
- Main Search now stores the query in the existing Browser session before editing the results. The existing `ghost_seen_v2_open` action resolves the active lifecycle session message while retaining the numeric real source chat ID in its callback payload.
- No new provider path, ToolRegistry, action, handler, Telegram client, prompt UI, or user-facing AI step was added.

### Files changed

- `backend/ai/session/request.py`
- `backend/ai/engine/dispatcher.py`
- `backend/bot/handlers/ghost_seen_v2.py`
- `tests/test_64_ghost_seen_v2_tool_isolation_search.py`
- `IMPLEMENTATION_REPORT.md`

### Regression coverage

- Ghost Seen AI requests disable tools and provider-emitted tool calls cannot execute Telegram or local tools.
- Normal owner AI requests still receive the existing tool definitions.
- Ghost Seen still generates and delivers a normal text reply with existing disclosure behavior.
- Search results preserve the active query, emit distinct source-chat callbacks, reach the single registered `ghost_seen_v2_open` action, resolve the real source chat, and navigate using the lifecycle panel session.

### Validation

- New focused regression tests: **5 passed**
- Ghost Seen v2 tests: **174 passed**
- Selected AI/tool and new capability-boundary tests: **66 passed**
- Full Python suite: **980 passed, 23 skipped, 1 warning**
- `python -m compileall -q backend`: PASS
- `git diff --check`: PASS
- `bun tsc -b --noEmit`: PASS
- Exactly one `INVESTIGATION.md` remains.
- No database schema, SQL, migration, deployment, or unrelated-system changes.
- Live Telegram/UI E2E was **not performed**; the Search callback was verified through the registered action/router boundary with mocked clients and lifecycle state.

### Git status

- Implementation commit: `8a3f70104be9fa200b5a5041eceb117dad99c311`.
- The implementation commit was pushed to `origin/main`; fetch verification confirmed `HEAD == origin/main` at that commit before this report metadata commit.
- The working tree was clean after that verification. This report correction is a related delivery-record update.

---

## Stage 8 — AI Reply execution-boundary verification and hardening

### Summary

Stage 7's production hardening was verified and extended only where Stage 8 required it. The owner flow remains `AI Reply → Context 1/5/10/20 → Disclosure Yes/No → automatic generation → automatic delivery`; no prompt, provider/model selection, confirmation, or Send UI was added.

### Files changed

- `backend/bot/handlers/ghost_seen_v2.py` — restored from `origin/main` after the prior placeholder incident, then retained bounded timeout, duplicate locking, strict result validation, final privacy/selection validation, and terminal cleanup.
- `tests/test_63_ghost_seen_v2_stage8.py` — added boundary regression coverage for duplicate isolation, timeout recovery, invalid results, delivery failure, disclosure, and bounded controls.
- `IMPLEMENTATION_REPORT.md` — this report.

### Stage 8 verification

- Duplicate callbacks are rejected before entering the active operation and cannot clear its state; the active operation owns cleanup.
- `asyncio.wait_for` cancels timed-out Engine execution; no late result reaches `send_reply`, and a fresh operation can run after the timeout.
- Ghost Seen contains no provider fallback; fallback remains exclusively in Engine → Dispatcher → ProviderManager.
- Provider/Engine failures, empty/non-string/whitespace/oversized results, and Telegram delivery failures are honest failures with zero or one attempted delivery and no automatic resend.
- Disclosure changes only the established suffix; the internal generation request remains unchanged.
- Context remains bounded to the validated source chat and selected message; no panel-chat or dialog enumeration is used.

### Validation

- Stage 8 focused tests: **11 passed**
- All Ghost Seen v2 tests: **169 passed**
- Full Python suite: **975 passed, 23 skipped, 1 warning**
- `python -m compileall -q backend`: PASS
- `git diff --check`: PASS
- `bun tsc -b --noEmit`: PASS

### Scope and limitations

- Exactly one `INVESTIGATION.md` exists.
- No database/schema, SQL, migration, deployment, or unrelated-system changes.
- You.com, web search, provider infrastructure, Save, Delete, Retrieve, Profile, and unrelated handlers were untouched.
- Live Telegram E2E was **not performed**; tests use mocks and no live account/provider action.

### Git status

Stage 8 changes are pending commit and remote verification.

---

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
