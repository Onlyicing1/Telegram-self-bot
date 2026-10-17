# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete (resolution reliability)

> This file describes ONLY the current state of the repository with respect to
> the Anti-Edit / Anti-Delete archive feature, including the **peer-resolution
> reliability fix** (single-flight deduplication + FloodWait cooldowns), and
> **replaces every previous report in full** — it is not an appended report.
> The earlier reports — the bot-exclusion round (`ad89419`), the archive-UX
> round (`33e1fd3`), the implementation round (`d862520`) and the inspection
> record (`cbeb6c1`) — remain readable in git history. Evidence-level records
> live in `docs/investigations/anti-edit-delete-status.md`.

| Item | Value |
|---|---|
| Stage | **Complete — AED resolution reliability delivered (round 4)** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin` = `https://github.com/Onlyicing1/Telegram-self-bot.git`) |
| Branch | delivery target `main`; working branch `premium-emoji-attribution` (tracks origin/main) |
| Base commit (round 4) | `ad89419acb02b479525765a39c6a388af2ba845e` — verified `= origin/main` at task start |
| Telethon | installed **1.43.0** — `FloodError` / `FloodWaitError(request, capture=N)` / `FloodPremiumWaitError` inspected directly in the installed package; the server-provided wait is carried on `.seconds` |
| Live Telegram / Render | **NOT verified.** No `.env`, no credentials, no deployment access. No Telegram request of any kind was made in this workspace |

---

## 1. Root cause (established by code inspection — the confirmed part)

`backend/services/anti_edit_delete_service.py::peer_bot_status` — the ONLY peer
resolution on the browse path (`filter_bot_items` → list AND item panels) — had
three concrete, source-verified gaps:

1. **Failures were uncached.** Any resolution failure (including
   `FloodWaitError`) raised, logged a debug line, and returned
   `False` = unknown WITHOUT any state. Every subsequent call re-attempted the
   RPC: repeated archive pagination/detail rendering of an unresolved peer
   produced a series of identical `get_entity` requests, matching the observed
   flood/request-storm pattern.
2. **No FloodWait handling.** `FloodWaitError` fell into the same generic
   `except Exception` branch as every other failure, so a server-ordered wait
   of 69 s was followed by an immediate retry on the next call — no respect for
   the duration, no cooldown.
3. **No single-flight.** Concurrent callers for the same unresolved peer each
   issued their own `client.get_entity` (only UI-panel renders run concurrently
   in this runtime, but nothing bound them).

The **unproven part** (kept out of the root cause): production-log items
(`backend.bot.handlers.private_message` `stage=get_sender_unresolved` FloodWait,
the keepalive `GetUsersRequest` failure, `QueryIdInvalidError`, Supabase
config-read errors) were NOT reproduced or fixed here. AED's own resolution
path is now cooldown-guarded so it can no longer contribute a repeated-`get_entity`
storm, but this task does not claim any of the unrelated errors is fixed.

## 2. What changed (exact files and reasons)

| File | Change | Reason |
|---|---|---|
| `backend/services/anti_edit_delete_service.py` | Peer-resolution reliability block (no API change; `peer_bot_status`/`filter_bot_items` signatures unchanged): *(a)* ONE shared in-flight resolution task per peer (`_inflight`, loop-scoped via `_inflight_registry()`, bounded at 512, completed tasks reclaimed only in the `finally` of a completed `asyncio.shield` await so a cancelled joiner never reclaims a task others share); *(b)* bounded temporary cooldown map `_cooldowns` keyed per chat id (`_defer_resolution` FIFO + expired-first eviction, bounded at 512); *(c)* FloodWait handling — `telethon.errors.FloodError` (covers `FloodWaitError` + `FloodPremiumWaitError`) is deferred for the server duration `.seconds`, capped at `_PEER_FLOOD_MAX_S = 3600` (an absurd wait is reported and the peer resolution is deferred, never retried early); a flood wait without a parseable duration falls back to the bounded `_PEER_FLOOD_DEFAULT_S = 60`; *(d)* transient failures get the short `_PEER_FAIL_COOLDOWN_S = 30` cooldown instead of an immediate re-attempt; *(e)* `reset_in_memory()` additionally clears `_inflight`, the loop marker and `_cooldowns`; module docstring's Scope section unchanged in spirit, extended for the reliability contract. | Deduplicate identical lookups, respect server-ordered waits, prevent request storms from repeated browsing, and keep cooldowns bounded/timed rather than permanent poison |
| `tests/test_anti_edit_delete.py` | **+10 tests (72 → 82)**, new section 15 (stub resolver recording every call; the REAL `FloodWaitError` built with `capture=N`): one shared resolution for 5 concurrent callers; no immediate retry during a 69 s flood wait (follow-up resolver never called) with a bounded, timed cooldown ≤ 69 s; a 99999 s wait capped and deferred within the 3600 s deadline; a 6× transient-failure storm collapsing to ONE attempt; failures never permanently poison (cooldown expiry → normal classification); flood-cooldown expiry → normal classification; a later successful resolution hides a persisted legacy bot row (human row stays); a cancelled caller leaves the shared task alive and the registry empty afterwards; reset/bounds behaviour for cache, cooldown map and task registry; unknown stays unknown (no heuristic upgrade). Fixed an initial test-stub flaw (stub returned the bot entity for every chat id; now chat-id-aware, mirroring real `get_entity`). | Pin the dedup, FloodWait, cooldown, cache/bounds and tri-state contracts |

Not changed: capture handlers, `_resolve_peer_identity`, listing/detail
rendering (they already call the shared `filter_bot_items`), row persistence,
`filter_bot_items` semantics, grouping/identity/pagination/retention, router
wiring, db helpers, and every other module. **No scheduler, worker, update
loop, persistence system or runtime-recovery owner was added; no env var; no
Supabase change.**

## 3. Behaviour contract after the fix

* **Cache layers (all bounded at 512, all cleared by `reset_in_memory`):**
  `_peer_bot_cache` (successful classifications), `_cooldowns`
  (chat id → monotonic expiry, after a failure or flood), `_inflight`
  (chat id → one shared task, loop-scoped, done-entries reclaimed on demand).
* **Request flow for one chat id:** cached success → returned offline;
  cold → active cooldown → no RPC (deferred, unknown); cold → in-flight task →
  joined via `asyncio.shield` (one network call for any number of callers);
  otherwise → one task created. Expiry prunes lazily, so cooldown state can
  never stick permanently.
* **FloodWait:** the wait is honoured as a per-peer cooldown equal to the
  server duration (≤ 3600 s), no sleep in the event loop, no scheduled retry
  task; each later caller during the cooldown simply gets `False` (unknown).
  One warning per shared attempt — callers never repeat it.
* **Tri-state classification unchanged:** resolved `bot=True` → bot (excluded);
  resolved human → human; failed/deferred/unsupported → unknown (rows kept).
  No row is deleted for an unresolvable peer and no permanent suppression rule
  exists.
* **Capture** handlers still classify from the event's own entity (offline
  Telethon cache, no extra RPC) and remain the earliest exclusion boundary;
  browse exclusion still covers both listing and direct item access through
  the shared helper.

## 4. Verification actually executed (local only)

| Check | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_anti_edit_delete.py -q` | **82 passed** |
| `.venv/bin/python -m pytest tests/ -q` (full suite) | **6381 passed, 27 skipped, 0 failed, exit 0** (27 skips are the pre-existing opt-in/live tests) |
| `.venv/bin/python -m compileall` on the two touched Python files | **exit 0** |
| `git diff --check` | **clean** |
| Telethon API verification | Installed 1.43.0 source: `FloodWaitError.__init__(self, request, capture=0)` sets `.seconds` (verified by instantiation) |
| Push / remote verification | Verified after push: `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor` — see §7 |

## 5. What was NOT tested or verified

* **No live Telegram and no Render verification.** Nothing was deployed; the
  running service's logs were not inspected. The fix is verified against the
  real code paths with deterministic mocks (real `FloodWaitError` type, stub
  resolvers) — mocked tests do not prove live behaviour.
* The unrelated production errors named in the brief (private-message
  FloodWait, keepalive `GetUsersRequest`, `QueryIdInvalidError`, Supabase
  config reads) were not investigated beyond the minimum needed to confirm AED
  is not duplicating their request pattern; **this task does not claim to have
  fixed them.** AED's own path can no longer contribute a repeated-`get_entity`
  storm, which is the only evidence-backed statement here.
* The `_PEER_FLOOD_MAX_S` deferral path (> 3600 s waits) is exercised by a test
  with a synthetic 99999 s wait only.
* The in-flight cache-ttl/eviction behaviour under a real multi-hour deployment
  profile (eviction churn with > 512 unique peers) is bounded by construction
  but not observed live.

## 6. Remaining limitations / risks

* A genuinely unreachable peer (entity permanently unresolvable) will retry
  once per cooldown expiry (30 s apart, only when archive browses actually
  request it) — deliberate: a permanent negative cache would be exactly the
  poisoning rule the brief forbids, and early-retry is what caused the storm.
* FloodWait deferral is per peer, not per RPC family: a server-wide
  `FLOOD_WAIT` triggered by a different subsystem (e.g. private-message
  handler) can still make Telegram reject this feature's next `get_entity`
  until the shared session-level limit clears; no cross-subsystem payment
  authority was added (that would be a new scheduler/owner, out of scope).
* `docs/investigations/anti-edit-delete-status.md` was not touched: its round-3
  record of the exclusion boundary remains accurate; the reliability contract
  is documented here.

## 7. Delivery state

* **Commit:** one commit on `premium-emoji-attribution` (tracks origin/main),
  pushed as `HEAD:main`; task-scoped files only.
* **Remote verification:** `git fetch origin` → `origin/main` equals the commit
  SHA; `git merge-base --is-ancestor` exit 0; `ls-remote` matches; 0 ahead /
  0 behind. Final SHA reported in the task summary.
* **Working tree:** clean except the pre-existing unrelated changes preserved
  unstaged/untouched: `M backend/services/premium_emoji_inline_service.py`,
  untracked `backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/`.
* **Explicit confirmation:** the Supabase schema was NOT changed, no SQL was
  executed, and `DATABASE_ARCHITECTURE.md` was NOT modified.

## 8. Next stage

`Complete` for round 4. Only the owner can perform live verification:
from a normal account open the archive list repeatedly while Telegram is
rate-limited (or watch one resolved peer), confirm the binds remain bounded
(one lookup per peer per cooldown window), and confirm with real entity
classification still resolves the peer at expiry. Any further work — e.g. a
cross-subsystem flood-state surface — is a separate, owner-authorized task
(that would be a new scheduler/owner, out of scope for this fix).