# Implementation Report — Ghost Seen v2 Stage 1

## Scope

Implemented only the new Stage 1 private-chat browser. AI, replies, selection, action menus, context, disclosure, generation, delivery, and Message Viewer were not implemented.

## Files changed

- `backend/services/ghost_seen_v2.py` — Telegram private-user filtering, tolerant name/username search, bounded pagination, two-line row formatting, and browser rendering.
- `backend/bot/handlers/ghost_seen_v2.py` — Stage 1 panel/inline registration and Telegram-dialog loading.
- `backend/bot/router.py` — registers the new Stage 1 handler.
- `backend/bot/handlers/misc.py` — adds the Ghost Seen v2 navigation entry.
- `tests/test_52_ghost_seen_v2_stage1.py` — focused Stage 1 tests.

## Behavior

The browser reads the live Telethon dialog stream and includes only non-bot, non-self Telegram users. Groups, supergroups, channels, service entities, and bots are excluded at the source layer. Search matches first name, last name, and username only; whitespace, concatenated names, case, and optional `@` are normalized for matching without changing display names. Rows render exactly two content lines with bounded previews, compact timestamps, plain numeric unread counts, and at most five chats per page.

The screen has the compact Ghost Seen header/status and cute watcher text. It has no manual Refresh control. Empty state is rendered without refresh. The current Stage 1 registration provides the Search control; Message Viewer and later interaction stages are intentionally deferred.

## Removed-feature checkpoint

The prior removal commit remains intact: old Ghost Seen modules, callbacks, input state, AI reply flow, and legacy prompt producer are absent. Shared panel/input infrastructure, AI providers, You.com/web search, Supabase infrastructure, and unrelated handlers remain untouched. No database migration, environment configuration, or Render configuration was changed.

## Validation

- Stage 1 focused tests: **6 passed**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.
- Telegram live E2E was not performed.

## Delivery

Final validation and diff review passed. The Stage 1 changes are ready for the repository delivery checkpoint.
