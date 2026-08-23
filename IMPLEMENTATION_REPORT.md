# Implementation Report — LifeOS Telegram Self-Bot

## Execution 26 — Ghost Seen Completion (Font System, Source Validation, Reply Flows, Disclosure, Removal, Persistence)

### Task / Result

Continued from clean remote state `0b8dc4f` (Execution 25). This execution made the
remaining Ghost Seen + font behavior REAL in production code:

1. **Font system hardening** — verified the existing 24-key allow-list registry
   (`backend/helper/font_style.py`, 23 styled fonts + default), its persistence through
   the column-per-setting `panel_settings` model, central application to every Glass UI
   surface via `panel_render._style()`, and added an idempotent migration for its two new
   settings columns plus focused regression coverage (protected tokens, restart safety,
   invalid fallbacks, button-label styling with byte-identical callback data,
   `.menu` dispatch independence).
2. **Ghost Seen source boundary** — the incoming listener now delegates to the service's
   authoritative `validate_private_source()`: bots, owner self-chat/Saved Messages,
   groups/channels/supergroups (non-positive chat ids), chat/sender mismatches, and
   missing senders are rejected at the REGISTRY boundary (not merely hidden in UI).
3. **Reply/action flow (Phases 3–4 of the contract)** — selecting exactly one message now
   opens a Glass UI menu showing an unambiguous REPLY TARGET banner (`format_reply_target`)
   with explicit choices: Reply myself (quote / no quote) and Reply with AI → context
   choice (1 / 5 / 10 / 20 messages, target included; N encoded in callback data and
   re-validated against `ALLOWED_CONTEXT_COUNTS`) → disclosure choice (recipient informed /
   not informed) → compose prompt. Execution fetches EXACTLY N messages ending at the
   anchor via `fetch_context_window` (passive reads only), runs through the single existing
   engine path, and appends `AI_DISCLOSURE_SUFFIX` ONLY when "informed" was chosen. The
   legacy multi-select AI path is unchanged (no suffix, selected messages as context).
4. **Registry management (Phase 5)** — "🗑 Remove from list" action deletes the registry
   row and local UI state only (`remove_chat`); Telegram chats/messages/read state are
   never touched. Retention stays wired via `ghost_seen_retention_days` (1..365, default 30).
5. **Real bug fixes found during wiring** — `ghost_open` never recorded the open chat
   (all toggle/page/clear/actions operated on chat `0`): fixed by `_set_current_chat(target)`;
   reply buttons pointed at non-existent inputs (`input:ghost_reply`) and silently dropped:
   repointed to the registered `input:ghost_chat:*` ids; `set_reply_disclosure` accepted
   non-boolean values: now strictly `isinstance(bool)` validated; migration constraint
   DDL was not re-runnable: now drops constraints before adding them.
6. **Naming compliance** — active surfaces renamed Ghost Room→Ghost Seen: database panel
   text, stats log key (`ghost_seen_chats`), db-client docstrings, handler env-accessor
   helper (`_ghost_seen_env_id`), migration comment anchors, test labels. `GHOST_ROOM_ID`
   remains the unchanged deployment variable.

Fully implemented on the code side. Live Telegram E2E and live Supabase application are
owner actions (below).

### Files actually changed

Production:
- `supabase/migrations/20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`
  (NEW) — idempotent: adds `dashboard_font text NOT NULL DEFAULT 'default'` and
  `ghost_seen_retention_days integer NOT NULL DEFAULT 30` to `panel_settings`,
  normalizes out-of-range values, seeds the singleton row, adds CHECK constraints
  (with `DROP CONSTRAINT IF EXISTS` guards so it is safe to run repeatedly).
- `backend/bot/handlers/ghost_seen.py` — listener now uses `validate_private_source`;
  `_ghost_open_action` sets the working chat; chat-view controls redesigned
  (single-selection → "⚡ Reply / Actions"; multi-selection → direct AI-prompt input;
  remove-from-list always available); NEW actions `ghost_actions`, `ghost_ctx`,
  `ghost_inform`, `ghost_remove` replacing dead `ghost_ai_single`/`ghost_ai_multi`;
  `_ghost_ai_input` consumes a complete reply flow (anchor+count+disclosure) via
  `fetch_context_window`, appends the disclosure suffix only when informed, keeps the
  legacy selected-messages path byte-for-byte, fails closed on missing `GHOST_ROOM_ID`
  (and cancels the pending flow); broken input button ids fixed; docstrings/logs renamed.
- `backend/services/ghost_seen_service.py` — `set_reply_disclosure` strict boolean
  validation (one-line fix; all other service primitives already existed and were made
  reachable, not duplicated).
- `backend/db/client.py` — two docstrings Ghost Room→Ghost Seen (function names were
  already table-named `count_ghost_chats`).
- `backend/services/database_service.py` — panel line "**Ghost Seen chats:**";
  stats-log key `ghost_room_chats`→`ghost_seen_chats`.
- `supabase/migrations/20260822090000_create_ghost_chats_table.sql` — comment header
  aligned with the renamed §22 anchor (documentation-only, no DDL change).
- `DATABASE_ARCHITECTURE.md` — §6 corrected font allow-list reference +
  `ghost_seen_retention_days` row; §6/§19.3/§20 document migration 20260823120000 as
  PENDING manual application; §17 settings table completed (13 columns);
  §22 retitled "Ghost Seen (ghost_chats)" with explicit source-registry-only semantics.

Tests:
- `tests/test_49_ghost_seen_flows.py` (NEW, 30 tests) — validator matrix (bot/self/
  group/mismatch/missing rejected, human private accepted), listener-delegates-to-validator
  pin, reply-flow state machine, exact-N context counting (+cap, +unresolvable anchor),
  REPLY TARGET banner honesty, menu callback-state encoding, removal semantics,
  open-chat regression, AI delivery (informed/uninformed/legacy/fail-closed/failed-fetch),
  no-second-dispatcher pin.
- `tests/test_50_font_system.py` (NEW, 14 tests) — ≥20 fonts + default, deterministic
  transforms, protected tokens across ALL keys (IDs/code/URL/digits/Persian),
  invalid/missing fallbacks, single-registry pin vs settings, button-label styling with
  byte-identical callback data, missing-DB defaults, persist-and-reload,
  invalid-selection rejection, corrupted-cache reads, no-handler-styles-incoming-text,
  raw-text `.menu` regex pin.
- `tests/test_47_ghost_seen_entry.py` — pinned action list updated to the four new actions.
- `tests/test_45_ghost_seen.py` — stale helper import removed; Ghost Room labels updated.
- `tests/test_44_database_stats.py` — pins updated to renamed panel text/key.

### Exact behavior changed

- Incoming private messages from bots/self-chat/non-human sources never create or update
  `ghost_chats` rows anymore (previously only the owner was excluded).
- Opening a chat actually tracks the open chat; selection/paging/reply/actions operate on
  the right conversation (previously chat `0` — silent breakage).
- Single-message selection opens an explicit menu instead of ambiguous immediate buttons;
  nothing executes without an explicit choice; AI context count travels in callback data
  and is re-validated server-side; disclosure is a required explicit choice; the AI-sent
  message ends with the AI-disclosure notice only when "informed" was selected.
- Manual replies target `GHOST_ROOM_ID` with correct quote/no-quote modes via registered
  input ids (previously the buttons silently did nothing).
- Remove clears the registry row + local state only; retention expiry unchanged.
- Invalid context sizes / disclosure choices render honest error views and cancel the
  pending flow deterministically.
- Database panel shows "**Ghost Seen chats:**" and logs `ghost_seen_chats`.

### Intentionally untouched

- Provider retry/fallback, dispatcher, telemetry, token accounting, memory, Save/Delete/
  Retrieve, RuntimeSupervisor, watchdog, profile engines, ai_usage/ai_provider_stats paths.
- Frontend `src/` — zero changes (tolerant unknown-font-key fallback verified in Exec 25
  and pinned by test).
- Documented-dormant legacy names kept deliberately: `runtime/startup_check._check_ghost_room`
  (dormant module per INVESTIGATION.md) and historical documents
  (`INVESTIGATION.md`, `docs/implementation/ghost-room-ai-foundation-contract.md`).
  Test helper names derived from the env var (`_patch_ghost_room_id`, …) kept — they
  manipulate `GHOST_ROOM_ID`, which is the sole sanctioned exception.

### Database / schema impact

Migration FILES exist (`20260822090000_create_ghost_chats_table.sql` — pre-existing;
`20260823120000_add_dashboard_font_and_ghost_seen_settings.sql` — new this execution).

**LIVE SUPABASE MIGRATION WAS NOT EXECUTED BY THIS AGENT.** No SQL was run against any
live database; no schema was verified live. The runtime degrades safely without both
(empty registry reads, default font/retention). The owner must apply both migrations
manually (both are idempotent).

### Validation

- `tests/test_49_ghost_seen_flows.py` — 30 passed
- `tests/test_50_font_system.py` — 14 passed (standalone AND suite; one order-dependency
  found and fixed before delivery)
- Focused set `test_45 + test_47 + test_48 + test_49 + test_50` — 92 passed (pre-fix run:
  5 failed → root-caused: 3 test bugs, 1 real service validation gap fixed, 1 missing
  flow precondition in my own test)
- Full suite — **838 passed, 0 failed, 1 warning** (pre-existing Starlette
  PendingDeprecationWarning)
- `python3 -m compileall -q backend` — PASS
- `git diff --check` — PASS
- Stale-name search — remaining hits limited to documented-dormant `startup_check`,
  historical docs, and env-var-derived test helper names (justified above); zero active
  feature identifiers use ghost_room/ghost_sink
- Duplicate-registration search — exactly one `register_panel("font")`, one
  `register_panel("ghost_seen")`, one `register_panel("ghost_chat")`; router registers
  the module once
- Frontend typecheck/build — NOT RUN: no frontend files changed

### Validation limitations / known remaining work

- Live Telegram end-to-end behavior (real incoming chats, real delivery to
  `GHOST_ROOM_ID`, real panel rendering) unverified — requires the deployed environment.
- **Owner must apply BOTH pending Supabase migrations manually** (see above); until then
  Ghost Seen registry rows and font/retention persistence fall back safely.
- Multi-select (>1) AI retains its pre-existing semantics (selected messages as context,
  no disclosure prompt) — extending the disclosure flow there was intentionally deferred
  to keep this execution within its contract.
- Glass UI font styling is Latin-only by design; Persian renders in the system font
  (disclosed in-product rather than claimed as Persian support).

### Commit / push / remote verification

- Implementation commit: `b58696275975187e4a55ab98c869914f64b67725`
  ("feat: complete Ghost Seen flows, source validation, and font persistence migration").
- Report commit: created immediately after this file was finalized with the hash above.
- Both commits pushed to `origin/main`; `git fetch origin` performed; local HEAD
  verified equal to `origin/main`; final working tree clean.

### Stop

Execution 26 complete. No further chunks started.
