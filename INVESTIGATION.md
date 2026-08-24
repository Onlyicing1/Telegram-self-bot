# Ghost Seen v2 Investigation

## Clean rebuild boundary

The previous Ghost Seen implementation was removed before this work. Its dedicated handler/service, legacy callbacks, selection/reply/AI state, input registrations, Ghost Room delivery path, and old UI are not reused. This execution begins the v2 rebuild with Stage 1 only: a private-chat browser.

## Stage 1 source and filtering

`backend/services/ghost_seen_v2.py` is the Stage 1 domain module. It reads actual Telethon dialogs through the self client and filters at the source layer using `is_private_user_dialog()` / `filter_private_dialogs()`. Only Telegram `User` entities that are neither bots nor the owner are eligible. Non-user dialogs, groups, supergroups, channels, service entities, saved messages, and self are excluded.

Search is limited to first name, last name, and username. Matching uses case-folded whitespace normalization, supports concatenated first/last names, and treats `username` and `@username` equivalently. Display values are not mutated. `page_items()` caps pages at five chats and `render_chat_row()` produces two lines with bounded previews, compact times, and plain numeric unread counts.

## UI and lifecycle

`backend/bot/handlers/ghost_seen_v2.py` registers the Stage 1 panel and loads dialogs from the existing Telethon client. `backend/bot/router.py` wires it into normal registration, and the main menu exposes `panel:ghost_seen_v2`. No AI, reply, message selection, action menu, context, disclosure, delivery, or Message Viewer path exists in Stage 1. No second watcher/event bus or persistence layer was introduced; Stage 1 reads current Telegram dialog state when rendered.

There is exactly one investigation document: this file. The old Ghost Seen database migration/table, if previously applied externally, was not destructively altered by this rebuild. No Ghost Room environment or Render configuration was added.

## Stage 2 source tracing

The browser action carries the selected `PrivateChat.chat_id` in `action:ghost_seen_v2_open:<id>`. The handler validates that ID against a fresh private-user dialog load, then calls `load_viewer_messages()` with that source ID. The panel/callback chat is used only as the inline panel session key; it is never passed to Telethon as the message source. Viewer state stores the source ID, display name, and page in the existing panel session navigation stack. Back uses the existing `panel:ghost_seen_v2` navigation target, removing the viewer layer and preserving browser state.

`load_viewer_messages()` requests a bounded number of messages from the selected source peer, filters unusable IDs, converts text/caption/media/unsupported content to safe display text, sorts chronologically, and exposes bounded pages. No persistence, AI state, reply state, or owner-input state was introduced. No manual refresh or parallel watcher system was added.

## Stage 3 selection tracing

The viewer keeps panel chat identity separate from source chat identity. The selection callback carries `source_chat_id|message_id`; the service stores only the real message IDs in an in-memory map keyed by source chat ID. The viewer session stores source ID, display name, and page in the existing navigation stack. Pagination reloads the same source and rehydrates selected IDs; opening a chat clears that source's previous selection. Clear removes only the current source selection. No AI, Reply, Action Menu, prompt, or delivery state is involved.

## Verification

Focused Stage 1 tests cover private-user filtering, bot/non-user exclusion, tolerant search, missing name fields, two-line rendering, truncation, pagination, empty state, and absence of a Refresh control. Telegram live E2E was not performed. Full-suite validation and delivery are recorded in `IMPLEMENTATION_REPORT.md` after completion.
