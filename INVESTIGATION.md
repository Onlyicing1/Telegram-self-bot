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

## Stage 4 Action Menu tracing

The viewer exposes `action:ghost_seen_v2_actions:<source_chat_id>` only when the source-keyed selection map is non-empty. The callback validates that the source has current selected IDs and that the viewer session resolves to the same source before pushing the Action Menu view. The menu carries the source ID and selected ID set in the existing session navigation stack. Reply and AI Reply callbacks call only `action_placeholder()`; they do not create input state, invoke AI, or send Telegram messages. An invalid or stale action callback returns a safe closed state. Back returns to the viewer without changing selection.

## Stage 5 Reply tracing

Reply is armed only from the Action Menu. `action:ghost_seen_v2_reply:<source>` reaches `_reply_action`, which requires `reply_target(source)` to be exactly one message; zero or two-plus selections fail closed and never arm input. The reply state is stored in-memory in `backend/services/ghost_seen_v2.py` as `_reply_states`, keyed by the PANEL chat ID (the chat where the owner will type), containing `{source, message_id, panel_msg_id}`. The generic pending-input system (`set_pending` keyed by owner, chat-scoped listener) consumes the next owner message only in that panel chat; the handler then `consume_reply(panel_chat)` and re-checks that the source still has exactly that one message selected.

Delivery uses the existing typed wrapper `backend.telegram_api.messages.send_message(client, source, text, reply_to=message_id)` — the destination is the source private chat and the target is the exact selected Telegram message ID. Panel chat, display name, page, and username are never used as the destination. On success the selection and reply state are cleared; on failure the state is consumed (no double send) and an honest error is shown. Cancel and Actions-Back clear the reply state; opening another chat clears it too. A stale typed message or stale callback therefore cannot send to a different source. AI Reply remains the Stage 4 inert placeholder via `action:ghost_seen_v2_placeholder:ai_reply:<source>`.

The old Ghost Seen implementation remains fully removed: no `input:ghost_chat:ai_prompt`, no `ai_prompt`, no `ghost_actions`/`ghost_ctx`/`ghost_inform`, no `GHOST_ROOM_ID`, and no legacy prompt literal exist in the v2 production modules (source-verified).

## Verification

Focused Stage 1 tests cover filtering, search, rendering, pagination, and empty state; Stage 2 covers viewer loading, bounding, and isolation; Stage 3 covers selection; Stage 4 covers the Action Menu placeholders; Stage 5 covers reply-target cardinality, panel-chat keyed reply state, stale-consumption fail-closed, cancellation, and the absence of any legacy prompt path. Telegram live E2E was not performed. Full-suite validation and delivery are recorded in `IMPLEMENTATION_REPORT.md` after completion.
