# Implementation Report — LifeOS Telegram Self-Bot

## Execution 27 — AI Flow Streamlining, Retention Durations, Font Coverage, PV-Message Root Cause, `Menu` Command

### Task / Result

Implemented the five requested fixes on top of Execution 26:

1. **Ghost Seen AI reply flow** — removed the redundant compose-button confirmation step. Choosing the disclosure option now arms the AI-prompt input directly; the user just types. Context-count selection and disclosure choice remain mandatory steps; nothing about the AI engine/dispatcher path changed.
2. **Ghost Seen retention** — converted the days-only setting into a seconds-based duration (`ghost_seen_retention_seconds`), added a Glass UI panel (Settings → ⏳ Ghost Seen Retention) with six presets (30 minutes / 2 h / 12 h / 1 d / 7 d / 30 d) plus a custom-minutes input, and wired lazy expiry to the configured window.
3. **Font coverage** — Bio and Username state/set pages now render their content values (mood, text, last bio/name, preview) as plain stylable text so the selected Glass UI font applies. Templates stay in code spans (`{var}` tokens must never be restyled). AI response text is verified by test to reach the destination byte-identical (default font only).
4. **Missing-PV-messages root cause** — identified and fixed: with a `StringSession`, the Telethon entity cache is empty after every restart, so bare-ID `get_messages`/`iter_messages` raise "Could not find the input entity" for any chat the account has not touched in this process lifetime. Chats seen since startup worked; older ones silently rendered empty because `fetch_chunk` swallowed all exceptions into `[]`. Fix: new `ensure_entity()` resolves the peer first and, on cache miss, performs one passive regular+archived dialogs sweep to repopulate the session cache before retrying. Fetch failures are no longer disguised as empty conversations — the panel renders honest "temporarily unavailable" / "could not load" / genuinely-empty states.
5. **`.menu` → `Menu`** — the single textual command is now the literal word `Menu` (`^Menu$`). No hidden `.menu` alias remains in active code. Command matching happens on raw outgoing text and is independent of the selected decorative font.

Fully implemented and validated at the unit/regression level. Live Telegram behavior was NOT exercised (no live client available to this agent).

### Starting commit

`a2dd18defcd488b85651ec86e9285875738371c5` (clean tree, origin/main verified)

### Files actually changed

Production:

- `backend/services/ghost_seen_service.py` — added `ensure_entity()` (cache check → passive archived+non-archived dialogs sweep → retry); `fetch_chunk()` now returns `(messages, error)` with honest `"entity"`/`"fetch"`/empty states; `fetch_context_window()` resolves the entity before fetching; `apply_retention(rows, retention_seconds)` clamps to [30 min, 365 days]. Registry-only semantics unchanged.
- `backend/bot/handlers/ghost_seen.py` — list handler passes retention seconds; chat panel renders honest error/empty/retry states (plus non-numeric chat-id guard); reply-target banner path resolves the entity first; `_ghost_inform_action` arms the registered `ai_prompt` pending input directly via `input_state.set_pending` (same handler/prompt as the old button) instead of offering an extra compose-button press.
- `backend/services/settings_service.py` — replaced `ghost_seen_retention_days` (1..365) with `ghost_seen_retention_seconds` (default 2 592 000 s = 30 days; validator 1800..31 536 000); added `RETENTION_PRESETS`, `format_duration()`, `is_retention_preset()`; deterministic default fallback on missing/corrupt values preserved.
- `backend/bot/handlers/misc.py` — command pattern `^.menu$` → `^Menu$`; docstring updated; Settings panel gains the ⏳ Ghost Seen Retention row; new `ghostret` panel + `ghostret_set:<seconds>` action (preset allow-list enforced) + `settings:ghostret_minutes` input handler.
- `backend/services/bio_service.py`, `backend/services/username_service.py` — `do_show` renders mood/text/last-bio(last-name)/preview as plain text (font-stylable); template/status/server-time remain code spans.
- `backend/bot/handlers/bio.py`, `backend/bot/handlers/username.py` — Set Text / Set Mood current-value lines render plain (stylable) instead of inside backticks.

Database:

- `supabase/migrations/20260823130000_ghost_seen_retention_duration.sql` (NEW) — idempotent: adds `panel_settings.ghost_seen_retention_seconds bigint NOT NULL DEFAULT 2592000`; backfills from legacy `ghost_seen_retention_days × 86400` and drops that column only while it still exists (guarded DO block); clamps out-of-range values; adds CHECK constraint 1800..31536000.

Documentation:

- `DATABASE_ARCHITECTURE.md` — §6 column table updated to seconds; §6 migration-status note, §17 settings table, §19.3 resolution text, §20 inventory: migrations #10/#11 marked Applied (verified by owner), #12 added as Pending manual application.
- `AGENTS.md` — `.menu` references replaced with the `Menu` command (overview, §5 table incl. pattern + font-independence note, repository layout comment, Save flow).
- `backend/bot/handlers/save.py` — docstring flow line only.

Tests:

- `tests/test_51_execution27.py` (NEW, 30 tests).
- `tests/test_45_ghost_seen.py` — retention tests moved to seconds semantics + sub-day-window case.
- `tests/test_47_ghost_seen_entry.py` — menu-row pin narrowed to `panel:ghost_seen` (the new `panel:ghostret` row is unrelated navigation).
- `tests/test_49_ghost_seen_flows.py` — compose-button assertion replaced by direct-input arming assertions; context-window fake clients now satisfy the entity-resolution precondition.
- `tests/test_50_font_system.py` — retention accessor pins moved to seconds; Menu regex source pin updated.
- `tests/test_12_save_engine.py` — settings-panel row count 11 → 12 (Font + Retention rows); docstring `.menu` → `Menu`.

### Exact behavior changed

- **AI flow**: select message → Reply/Actions → Reply with AI → choose context count (re-validated against the allow-list) → choose disclosure → type instruction immediately (input already armed). Execution, context windowing, disclosure suffix, and GHOST_ROOM_ID delivery are unchanged.
- **Retention**: expiry window is user-configurable down to 30 minutes; opening/refreshing the Ghost Seen list lazily removes expired registry rows only (Telegram data untouched). Invalid values fail safe to the previous value or the 30-day default; a corrupt value can never crash the panel.
- **Fonts**: Bio/Username content displays carry the selected font; AI output never does (pinned by test asserting byte-identical delivery under a script font).
- **PV messages**: previously-failing private chats resolve their entity via one passive dialogs sweep (regular + archived) and render normally; unresolvable chats show an explicit unavailable state; iteration failures show a retryable error state; truly empty chats say so honestly.
- **Command**: typing `Menu` opens the mother panel exactly as `.menu` did (same inline/fallback delivery); `.menu` no longer matches anything.

### Font behavior

Allow-list, deterministic fallback, restart-safe persistence, protected callback data/IDs/URLs/digits/Persian, byte-identical callback payloads — all preserved and still pinned by tests 42/48/50. New coverage proves Bio/Username display styling and AI-output exemption.

### Ghost Seen behavior

Source boundary (private humans only, bots/self/groups/channels rejected), selection/paging/removal, reply-target banner, context counting, disclosure suffix, fail-closed destination, zero-spam edit-in-place panels — unchanged and regression-tested.

### Intentionally untouched

Provider retry/fallback · token accounting · telemetry · ai_usage/ai_provider_stats persistence · memory · Save/Delete engines · RuntimeSupervisor · watchdog · other Telegram handlers · model selector · web dashboard frontend (no src/ change) · `GHOST_ROOM_ID` env name · dormant `runtime/startup_check.py` internal `ghost_room` naming (documented legacy, not called in prod) · live Supabase.

### Database / schema impact

Migration file `20260823130000_ghost_seen_retention_duration.sql` created (idempotent). **LIVE SUPABASE MIGRATION WAS NOT EXECUTED BY THIS AGENT.** Per the owner's verification recorded in this task, `ghost_chats`, `dashboard_font`, and `ghost_seen_retention_days` exist live; until the new migration is applied manually, the runtime's seconds-backed setting will fall back deterministically to its in-memory/30-day default on the live DB (the repository layer tolerates the missing column; writes degrade to the cache as designed). No SQL was executed against any database by this agent.

### Validation

- `tests/test_51_execution27.py` — 30 passed
- Focused suites (45/47/49/50/42/48/12) — 132 passed (after updates)
- Full suite `pytest tests/ -q --asyncio-mode=auto` — **869 passed, 0 failed, 1 warning** (pre-existing Starlette deprecation warning)
- `python3 -m compileall -q backend` — PASS
- `git diff --check` — PASS
- Stale-reference search: no active `.menu` refs in backend; Ghost Room/Sink/Sync strings only in documented-dormant `startup_check.py`
- Duplicate-registration search: `menu` and `ghostret` each registered once
- Frontend typecheck/build — NOT run: no frontend file changed

### Validation limitations

Live Telegram E2E (real entity sweep against production accounts, real helper-bot rendering) not exercised. Live Supabase migration not executed (see above). Order-dependence of the suite re-verified explicitly after fixing the registration guard (test_12 → test_51 ordering reproduced and resolved).

### Known remaining work

Apply migration #12 to the live database (manual owner action). Dormant `startup_check` module still uses internal `ghost_room` naming. Live Telegram smoke test of all five fixes recommended after deploy.

### Commit / push / remote verification

- Implementation commit: `PENDING`
- Report commit: `PENDING`
- Push result: `PENDING`
- Remote verification: `PENDING`
- Final working-tree state: `PENDING`

### Stop

Execution 27 complete. Not starting another chunk.
