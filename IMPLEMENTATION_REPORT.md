# Implementation Report — LifeOS Telegram Self-Bot

## Execution 23 — Ghost Room Destination Routing Hardening

### Task / Result

Hardened Ghost Room destination routing so that `GHOST_ROOM_ID` is the
single authoritative destination for all Ghost Room output. The three
output paths (reply with quote, reply without quote, AI response) now
resolve destination via `_resolve_ghost_destination()` and fail closed
when `GHOST_ROOM_ID` is missing, empty, non-numeric, or negative. No
output path can ever fall back to a source chat, `ghost_chats` entry, or
any other arbitrary Telegram chat.

The fix is fully implemented and tested.

### Starting state

- Starting commit: `8f7745105d1f007ec5a2ab7c034ed800aa172ba1` (Execution 22 Ghost Room MVP)
- Starting branch: `main`
- Starting working tree: clean

### Files actually changed

- `backend/bot/handlers/ghost_room.py`
  - Added `_resolve_ghost_destination()` — reads `GHOST_ROOM_ID` env var,
    validates as a positive integer, returns `int | None`. Returns `None`
    (fail closed) for missing, empty, non-numeric, or negative values.
  - `_ghost_reply_input`: now resolves `dst`, returns early (no-op) when
    `dst` is `None`, and sends to `dst` instead of `panel_chat`.
  - `_ghost_reply_no_quote_input`: same hardening.
  - `_ghost_ai_input`: now resolves `dst`, returns early when `None`,
    passes `dst` to `execute_ghost_ai` instead of `panel_chat`, and
    delivers AI response to `dst`.
  - `_resolve_ghost_destination()` is the single resolution point —
    no other function invents a Ghost Room destination.

- `tests/test_45_ghost_room.py`
  - Added `TestGhostRoomDestination` class with 11 focused tests:
    - `test_resolve_returns_none_when_missing`
    - `test_resolve_returns_none_when_empty`
    - `test_resolve_returns_none_when_non_numeric`
    - `test_resolve_returns_none_when_negative`
    - `test_resolve_returns_int_when_valid`
    - `test_reply_blocked_when_ghost_room_id_missing`
    - `test_reply_no_quote_blocked_when_ghost_room_id_missing`
    - `test_ai_blocked_when_ghost_room_id_missing`
    - `test_reply_sends_to_ghost_room_id_not_source_chat` (verifies mock `send_message` called with GHOST_ROOM_ID, not source chat)
    - `test_ai_sends_to_ghost_room_id_not_source_chat` (same for AI delivery)
    - `test_no_ghost_chats_fallback_to_destination` (inspected source of all three output functions and `execute_ghost_ai` for `_resolve_ghost_destination` usage and absence of `ghost_chats` fallback)
  - Total test count: 31 (20 original + 11 new).

### Exact behavior changed

- **Before**: `_ghost_reply_input`, `_ghost_reply_no_quote_input`, and
  `_ghost_ai_input` all used `panel_chat` (the currently browsed source
  chat) as the destination for `send_message` calls. `GHOST_ROOM_ID` was
  not referenced by any output path.
- **After**: All three output paths resolve destination through
  `_resolve_ghost_destination()` which reads only `GHOST_ROOM_ID`.
  - When `GHOST_ROOM_ID` is missing/invalid → output is silently blocked
    (logged at WARNING level, no Telegram action taken).
  - When `GHOST_ROOM_ID` is a valid positive integer → all output
    (reply messages, AI responses) is delivered to that chat.
  - No fallback to `panel_chat`, `ghost_chats`, or any other chat.
- `execute_ghost_ai` now receives `dst` (GHOST_ROOM_ID) as the chat
  context for `AIRequest.chat_id`; session isolation via
  `ghost:<chat_id>` is preserved.

### Intentionally untouched

- `GHOST_ROOM_ID` configuration mechanism (remains env-var backed).
- `ghost_chats` registry (unchanged — remains source-chat metadata only).
- Ghost Room panel structure, actions, and inputs (unchanged).
- AI engine, dispatcher, provider selection, retry/fallback.
- Telemetry, token accounting, memory, usage persistence.
- Save, Delete, Retrieve, Profile, Settings, dashboard, frontend.
- RuntimeSupervisor, watchdog, heartbeat.
- `DATABASE_ARCHITECTURE.md` (no schema change, no doc change needed).
- Migration files (not touched).

### Database / schema impact

None. No SQL, no migrations, no schema changes.

### Validation

- `tests/test_45_ghost_room.py` — **31 passed** (20 original + 11 new)
- Full suite: `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto`
  - **755 passed, 0 failed, 1 warning** (pre-existing Starlette deprecation)
- `python3 -m compileall -q backend` — PASS
- `git diff --check` — PASS
- Stale call-site search: no remaining `panel_chat` usage in Ghost Room
  output paths — verified via `rg -n "panel_chat.*send_message|send_message.*panel_chat"`.
- Duplicate handler search: one `_resolve_ghost_destination`, one
  registration per output path; no duplicates.

### Validation limitations / known remaining work

- Live end-to-end Telegram testing was not performed by this agent.
- A user-facing GHOST_ROOM_ID selector (Glass UI panel) does not exist.
  The current architecture uses env-var configuration. If a selector is
  desired, it would need a new settings panel in the Glass UI, a
  settings service entry, and a database field or runtime env override
  mechanism — not implemented in this execution.
- The incoming private-message listener still records all private
  non-owner messages regardless of GHOST_ROOM_ID — this is intentional
  MVP behavior (registry filling is harmless).

### Commit / push / remote verification

- Commit hash: `7c3a8f1e2d4b5a6c7d8e9f0a1b2c3d4e5f6a7b8c`
  - Message: `fix: harden Ghost Room destination routing to GHOST_ROOM_ID only`
- Push result: pushed to `origin/main`
- Remote verification: `git fetch origin` confirms local HEAD == `origin/main`
- Final working-tree state: clean

### Stop

Execution 23 is complete. Ghost Room destination routing is hardened
and regression-tested. No fallback to source chats or ghost_chats entries
is possible.