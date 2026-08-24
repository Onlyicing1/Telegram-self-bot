# Implementation Report — Ghost Seen v2 Stage 2

## Scope

Implemented Stage 2 Message Viewer on top of the existing Stage 1 private-chat browser. AI, replies, message selection, disclosure, generation, and delivery remain intentionally unimplemented.

## Files changed

- `backend/services/ghost_seen_v2.py` — existing Stage 1 behavior plus source-chat-isolated bounded message loading and viewer rendering.
- `backend/bot/handlers/ghost_seen_v2.py` — existing browser wiring plus dedicated viewer panel, chat-open action, viewer pagination, and Back navigation.
- `tests/test_53_ghost_seen_v2_stage2.py` — focused Stage 2 viewer tests.

## Behavior

The browser reads the live Telethon dialog stream and includes only non-bot, non-self Telegram users. Groups, supergroups, channels, service entities, and bots are excluded at the source layer. Search matches first name, last name, and username only; whitespace, concatenated names, case, and optional `@` are normalized for matching without changing display names. Rows render exactly two content lines with bounded previews, compact timestamps, plain numeric unread counts, and at most five chats per page.

The screen has the compact Ghost Seen header/status and cute watcher text. It has no manual Refresh control. Empty state is rendered without refresh. Selecting a private chat opens a dedicated Message Viewer using the selected Telegram source chat ID, never the panel chat ID. Messages are loaded through Telethon with a bounded request, rendered chronologically in bounded pages, safely represented for text/media/empty content, and navigated with Back plus older/newer controls only when needed. No Refresh, AI, or Reply controls are present.

## Removed-feature checkpoint

The prior removal commit remains intact: old Ghost Seen modules, callbacks, input state, AI reply flow, and legacy prompt producer are absent. Shared panel/input infrastructure, AI providers, You.com/web search, Supabase infrastructure, and unrelated handlers remain untouched. No database migration, environment configuration, or Render configuration was changed.

## Validation

- Stage 1 + Stage 2 focused tests: **10 passed**.
- Full suite: **816 passed, 23 skipped, 1 pre-existing warning**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.
- Telegram live E2E was not performed.

## Delivery

Stage 2 changes were validated locally and are ready for repository delivery. No Telegram live E2E was performed.
