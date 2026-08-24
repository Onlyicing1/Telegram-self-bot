# Implementation Report — Ghost Seen v2 Bounded Manage UI

## Scope

Replaced the unbounded `⚙ Manage` permission panel (which rendered roughly one inline button per private chat — ~500 buttons in one Telegram message) with a bounded, paginated, searchable Manage UI. At most 8 chat rows render per page. No AI, no legacy code, no provider/web-search/You.com changes.

## Files changed

- `backend/services/ghost_seen_v2.py` — added `MANAGE_PAGE_SIZE = 8` and `manage_page_items()` (bounded pagination, sorted by display name, reuses the existing `matches_search` tolerant search).
- `backend/bot/handlers/ghost_seen_v2.py` — Manage section rewritten: `_manage_buttons()` (≤8 chat rows + nav/search/back), `_render_manage()` (bounded body + buttons), `_manage_page_action()` (Previous/Next), `_manage_search_input_handler()` (panel-scoped `input:ghost_seen_v2_manage:search`), `_toggle_permission_action()` now preserves the current Manage page and query; `register()` wires `action:ghost_seen_v2_manage_page` and the Manage search input.
- `tests/test_58_ghost_seen_v2_manage_bounded.py` — 21 focused regression tests.

## Bounded Manage behavior

- **Never the full list**: one Manage page contains at most `MANAGE_PAGE_SIZE` (8) chat rows. A 500-chat account renders 63 pages, not 500 buttons.
- **Rows**: `N. 💬 <name>  ON|OFF` — numbered, with the current permission state.
- **Navigation**: `‹ Previous   p/total   Next ›` only when more than one page exists.
- **Search**: `🔎 Search` opens the existing tolerant search input (first/last name, username, `@`, case-insensitive, whitespace-tolerant) scoped to the Manage panel. Search results are also bounded/paginated.
- **Back**: `← Back` returns to the Stage 1 browser.
- **Toggle**: ON↔OFF preserves the current Manage page and query (reads `_session_extra` for `p=`/`q=`).
- **No Refresh button** anywhere in Manage.

## Preserved behavior

Stages 1–5 (browser, viewer, selection, Action Menu, Reply + Send-without-reply), per-chat privacy opt-in with `bot_settings` persistence, newest-first viewer ordering, source-chat identity, reply state, destination env vars, no manual Refresh, no legacy Ghost Seen (`input:ghost_chat:ai_prompt`, `ai_prompt`, `GHOST_ROOM_ID`, `ghost_actions`, `ghost_ctx`, `ghost_inform`), no AI, no provider/web-search/You.com changes.

## Destination configuration (unchanged)

```
GHOST_SEEN_DESTINATION_CHAT_ID=<numeric Telegram chat ID>
GHOST_SEEN_DESTINATION_CHAT_NAME=<user-defined display name>
```

`GHOST_SEEN_DESTINATION_CHAT_ID` is the authoritative numeric Telegram chat ID; `GHOST_SEEN_DESTINATION_CHAT_NAME` is display-only. Both are read from environment variables in `backend/config.py` and must be configured by the user in Render → Environment Variables. No deployment was performed.

## Validation results

- Focused Ghost Seen v2 tests (Stages 1–5 + hardening + Manage bounded): **102 passed**
- New Manage regression tests (`test_58`): **21 passed**
- Full Python suite: **908 passed, 23 skipped, 1 pre-existing warning**
- `compileall`: **PASS**
- `git diff --check`: **PASS**
- `bun tsc -b --noEmit`: **PASS**
- Legacy-identifier scan of v2 production modules: **clean**
- Exactly one `INVESTIGATION.md`: **confirmed**
- Telegram live E2E: **not performed**

## Delivery

Commit and remote verification completed after this validation.