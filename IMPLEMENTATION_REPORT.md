# Implementation Report — LifeOS Telegram Self-Bot

## Execution 25 — Ghost Seen Rename Completion + Glass UI Font Panel Hardening

### Task / Result

Continued from a clean remote state (`c73e882`, Execution 24). The working tree contained an uncommitted, incomplete feature chunk started outside this session:

1. **Ghost Room → Ghost Seen rename** — files were renamed but every internal reference was stale: `router.py` imported a nonexistent module (bot startup would crash), the handler imported the nonexistent `ghost_room_service` module and the old `execute_ghost_ai` name, the menu button targeted `panel:ghost_seen` while the handler registered `"ghost"`, and both renamed test files still asserted the old world.
2. **Glass UI Font panel** — Settings → 🔤 Font backed by a new `font_style.py` allow-list registry (23 styled fonts + default), wired into panel rendering.

This execution completed both coherently:

- Fixed the router import (`backend.bot.handlers.ghost_seen`) and registration key (`"ghost_seen"`).
- Repointed all ~10 service imports to `backend.services.ghost_seen_service` and all AI calls to `execute_ghost_seen_ai`; session isolation is now `ghost_seen:<chat_id>`.
- Removed three duplicated DB helpers from the handler (`_read_ghost_chats_sync`, `_upsert_ghost_chat_sync`, `_clear_unread_sync`) — the handler now delegates to the equivalent service functions (`read_registry_rows`, `upsert_source_chat`, `clear_unread`), restoring the services-hold-business-logic boundary.
- Wired deterministic lazy retention into the chat-list read: `apply_retention(rows, settings_service.ghost_seen_retention_days())` + best-effort `delete_expired_rows`. This makes the previously dead `ghost_seen_retention_days` setting (default 30, validated 1..365) functional.
- Fixed a latent crash: `format_chat_view_item` gained a required `owner_id` parameter in the WIP; the handler call site now passes `_store_owner_id`.
- Aligned user-facing titles with the renamed entry ("👻 Ghost Seen").
- Updated the renamed test files to the new modules/IDs and added new coverage: registry fallback via service functions, retention expiry/clamping, AI context-payload shape (`Conversation context:` / `Task:`), session-id pin (`ghost_seen:999`), and destination-routing guards (unchanged semantics, new names).
- Unified the font allow-list contract: `DASHBOARD_FONTS == FONT_KEYS` is pinned by test; the web dashboard tolerates unknown keys with a deterministic default-stack fallback (verified in `src/App.tsx`, unchanged).
- Added `tests/test_48_font_panel.py` (8 tests) covering the font panel: deterministic default, valid-set persistence, invalid-set rejection keeping the previous value, pagination bounds, single-registration integrity, allow-list-keys-only callback data.

Task fully implemented; nothing pending on the code side.

### Files actually changed

Production (all pre-existing user WIP plus this session's fixes):

- `backend/bot/router.py` — import + registration tuple now use `ghost_seen`.
- `backend/bot/handlers/ghost_seen.py` (renamed from `ghost_room.py`) — service imports repointed; `execute_ghost_seen_ai`; panel registered as `"ghost_seen"` (parent of `ghost_chat` updated); duplicated DB helpers deleted in favor of service delegation; lazy retention wired into the list read; `format_chat_view_item(..., _store_owner_id)` crash fix; titles "👻 Ghost Seen".
- `backend/services/ghost_seen_service.py` (renamed from `ghost_room_service.py`, expanded 230→587 lines in the WIP) — unchanged by this session except being made reachable; owns registry ops, reply-flow API, retention, context window, formatters, and the engine-path AI execution.
- `backend/bot/handlers/misc.py` — (WIP) 🔤 Font settings row + paginated font picker panel/actions; (WIP) menu label "👻 Ghost Seen".
- `backend/helper/font_style.py` — (new, WIP) authoritative enumerated font registry: `FONT_KEYS`, `apply_font`, code-span/URL/digit/Persian pass-through guarantees.
- `backend/helper/panel_render.py` — (WIP) applies the persisted font to titles/bodies/button labels via `_style()` with total-failure fallback to untouched text.
- `backend/helper/panels.py` — (WIP) input prompts are styled through the same `_style()`.
- `backend/services/settings_service.py` — (WIP) `DASHBOARD_FONTS` sourced from `FONT_KEYS`; new `ghost_seen_retention_days` setting (validated 1..365, default 30).

Tests:

- `tests/test_45_ghost_seen.py` (renamed) — repointed to new module/function names; registry-fallback tests retargeted to service functions; new `TestGhostSeenRetention` (expiry + clamping); payload/session assertions updated to the current honest shapes.
- `tests/test_47_ghost_seen_entry.py` (renamed) — pins `panel:ghost_seen` menu entry, single registration, dispatch-to-existing-handler, actions/inputs reachable, edit-in-place navigation, and untouched GHOST_ROOM_ID destination routing under the new names.
- `tests/test_42_dashboard_font.py` — valid-key examples moved to still-valid keys (`fraktur`, `small_caps`); new `test_allow_list_is_the_single_font_style_registry` pinning `DASHBOARD_FONTS == FONT_KEYS` and the frontend's deterministic fallback; invalid-value semantics preserved.
- `tests/test_12_save_engine.py` — settings-panel row count 10 → 11 (intentional new Font row).
- `tests/test_48_font_panel.py` — new (8 tests).

### Exact behavior changed

- Bot startup works again: router imports resolve; `.menu` shows "👻 Ghost Seen"; pressing it edits in place into the existing chat-list panel.
- All Ghost Seen output paths remain fail-closed on `GHOST_ROOM_ID`: missing/empty/non-numeric/negative → silent block, never a fallback chat; `ghost_chats` rows are source-chat registry entries only and can never become destinations (pinned by tests).
- Opening a chat clears its registry unread counter through the service; selection/AI/reply flows unchanged except names; AI requests carry `session_id="ghost_seen:<chat_id>"` for context isolation and go through `Engine.execute()` only.
- Reading the chat list lazily expires registry rows older than `ghost_seen_retention_days` (rows without timestamps are kept); expired rows are deleted best-effort; failures degrade to showing what was read.
- Glass UI text renders in the persisted font; digits, IDs (`S0001`, chat IDs), code spans, URLs, emoji/markdown markers, and Persian script pass through untouched; any styling failure falls back to plain text so rendering can never break.
- Invalid/missing persisted font values read back as `"default"`; invalid selections via the panel are rejected with the previous value kept.
- Web dashboard: unknown font keys fall back to the default CSS stack (pre-existing tolerant loader, now pinned by test).

### Intentionally untouched

- Provider retry/fallback policy, dispatcher, telemetry, token accounting, memory, Save/Delete/Retrieve, RuntimeSupervisor, watchdog, profile engines.
- `GHOST_ROOM_ID` ENV ownership and `_resolve_ghost_destination()` fail-closed logic (name kept; env var name unchanged).
- Supabase schema and migrations — none added or modified; no SQL executed.
- Frontend `src/` — zero changes.
- Legacy internal names kept where they are contractual or dormant: `database_service` stats key `"ghost_room_chats"` (dashboard API contract) and `startup_check._check_ghost_room` (documented-dormant module).
- Unwired service-layer API left as-is (documented remaining work): `remove_chat`, `validate_private_source`, `fetch_context_window`, reply-flow functions, `format_reply_target`.

### Database / schema impact

None. No SQL, no migrations, no schema changes in this execution. The `ghost_chats` table remains as documented in DATABASE_ARCHITECTURE.md; its migration artifact from Execution 22 remains pending manual application by the owner (runtime degrades safely to empty reads without it).

### Validation

- `tests/test_45_ghost_seen.py + tests/test_47_ghost_seen_entry.py` — 48 passed (after fixes; initial run 30 failed / 11 errors against stale names)
- `tests/test_42_dashboard_font.py + 45 + 47` — 58 passed
- Full suite (mid-run) — 785 passed, 1 failed (`test_settings_panel_renders` count) → fixed → full suite **794 passed, 0 failed, 1 warning** (pre-existing Starlette PendingDeprecationWarning)
- `tests/test_48_font_panel.py` — 8 passed
- `python3 -m compileall -q backend` — PASS
- `git diff --check` — PASS
- Stale-reference search: no `ghost_room_service` / bare `execute_ghost_ai` refs remain in `backend/`
- Duplicate-registration search: exactly one `register_panel("font")` (misc) and one `register_panel("ghost_seen")` (ghost_seen)
- Frontend typecheck/build not run — no frontend files changed

### Validation limitations / known remaining work

- Live Telegram end-to-end behavior (real incoming private chats, real delivery to `GHOST_ROOM_ID`) not verified — requires a live environment.
- `ghost_chats` migration still pending manual owner application (pre-existing).
- Service-layer API awaiting future wiring: per-chat removal action, private-source validation gate, wider context-window fetching, quote-reply flow enrichment, `format_reply_target`.
- Glass UI font styling is Latin-only by design; Persian text renders in the system font (disclosed in the panel body rather than claimed as Persian support).
- Web dashboard exposes only its own 4 CSS options; Glass UI offers the full 23+default list. Keys unknown to the web fall back deterministically.

### Commit / push / remote verification

- Commit: `1a7890953df3053c5b0bdcdfa0d2257ed0455023` — "feat: complete Ghost Seen rename and Glass UI font panel".
- Pushed to `origin/main`; `git fetch origin` performed; local HEAD verified equal to `origin/main`.
- Working tree clean after commit.

### Stop

Execution 25 complete. No further chunks started.
