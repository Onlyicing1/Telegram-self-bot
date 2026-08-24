# Implementation Report — Ghost Seen v2 Performance Fix

## Scope

Fixed the critical performance bottleneck in the Ghost Seen Browser: opening the Browser previously called `client.iter_dialogs()` to enumerate ALL Telegram dialogs (~500 for large accounts) even when only 2 chats were explicitly allowed. The Browser now resolves only the allowed chat IDs directly via `client.get_entity()`, making the cost O(allowed) instead of O(total dialogs).

## Root cause

```
Browser open → _render_browser() → load_allowed_chats() → load_private_chats() → client.iter_dialogs()
```

`load_allowed_chats()` called `load_private_chats()` which ran `client.iter_dialogs()` — a full Telegram dialog stream enumeration — then filtered by the allowed set. For an account with 500 dialogs and 2 allowed chats, this scanned all 500 dialogs to show 2.

## New call path

```
Browser open → _render_browser() → resolve_allowed_chats() → client.get_entity(allowed_id) for each allowed ID
```

The new `resolve_allowed_chats()` reads the allowed IDs from the in-memory `_allowed_chats` set (already loaded and cached on first access), then calls `client.get_entity()` only for those specific IDs. If the allowed set is empty, returns immediately with zero Telegram RPCs.

## Files changed

- `backend/services/ghost_seen_v2.py` — added `resolve_allowed_chats()` (O(allowed) entity resolution via `get_entity`, no `iter_dialogs`). `load_allowed_chats()` and `load_private_chats()` are preserved for the Manage path which intentionally performs broad dialog discovery.
- `backend/bot/handlers/ghost_seen_v2.py` — `_render_browser()` and `_search_input_handler()` now call `resolve_allowed_chats()` instead of `load_allowed_chats()`. The watcher count is read from `len(get_allowed_chats())` (zero Telegram calls). Manage handlers continue using `load_private_chats()` (intentional broad discovery).
- `tests/test_59_ghost_seen_v2_perf.py` — 15 performance regression tests.

## Key architectural decisions

| Path | Data source | Why |
|---|---|---|
| **Browser** (normal open) | `resolve_allowed_chats()` → `get_entity()` per allowed ID | O(allowed), never enumerates all dialogs |
| **Search** (Browser search) | `resolve_allowed_chats()` + `matches_search()` | Same O(allowed) path, no broad enumeration |
| **Manage** (privacy config) | `load_private_chats()` → `iter_dialogs()` | Intentionally discovers all private chats for the privacy toggle UI |
| **Message Viewer** | `client.iter_messages()` on the specific source chat | Already bounded by source chat, unrelated |
| **Watcher count** | `len(get_allowed_chats())` | Zero Telegram calls, reads from in-memory set |

## Allowed-chat loading and caching

- `_allowed_chats` is an in-memory `set[int]` loaded once via `_load_allowed_from_db()` on first access (`_ensure_allowed_loaded()`).
- The DB load reads the `bot_settings` table (key `ghost_seen_allowed_chats`, JSON-encoded list). The existing Supabase client is synchronous (`db.table(...).execute()`), but this runs once per process lifetime and is fast for a single small row.
- The in-memory set is the cache: `_allowed_chats` is updated in-place by `allow_chat()`/`disallow_chat()`, which also persist to DB. Process restart triggers a fresh DB load.
- The allowed set is never stale within a running process because all mutations go through `allow_chat()`/`disallow_chat()`.

## 500 dialogs + 2 allowed chats

| Metric | Before | After |
|---|---|---|
| Telegram RPCs on Browser open | 1 (iter_dialogs, streams ~500 entities) | 2 (get_entity × 2) |
| Entities processed | ~500 (all dialogs) | 2 (only allowed) |
| O(complexity) | O(total_dialogs) | O(allowed) |
| Empty allowed set | Still scans all dialogs | Zero Telegram calls |

## Preserved behavior

All Stages 1–5, Manage bounded UI, privacy opt-in, newest-first viewer, source-chat identity, reply modes, destination config, no Refresh, no legacy Ghost Seen identifiers, no AI, no provider/web-search/You.com changes.

## Validation results

- Focused Ghost Seen v2 tests (Stages 1–5 + hardening + Manage + performance): **117 passed**
- New performance regression tests (`test_59`): **15 passed**
- Full Python suite: **923 passed, 23 skipped, 1 pre-existing warning**
- `compileall`: **PASS**
- `git diff --check`: **PASS**
- `bun tsc -b --noEmit`: **PASS**
- Production-source inspection: Browser does not call `iter_dialogs()`; only Manage does
- Exactly one `INVESTIGATION.md`: **confirmed**
- Telegram live E2E: **not performed**

## Delivery

Commit and remote verification completed after this validation.