# Implementation Report — Ghost Seen v2 Stage 5/6 Hardening

## Scope

Hardened the existing Stage 1–5 Ghost Seen v2 implementation with per-chat privacy opt-in, newest-first message ordering, improved selection UX, dual reply modes, and configurable destination. No AI was added; no legacy code was resurrected.

## Files changed

- `backend/services/ghost_seen_v2.py` — per-chat privacy model (`_allowed_chats` set, `allow_chat`/`disallow_chat`/`is_chat_allowed`, DB persistence via `bot_settings`), `load_allowed_chats()` filter, `get_destination_chat_id()`/`get_destination_chat_name()` env config, `send_message_plain()` for send-without-reply, `begin_reply`/`consume_reply`/`get_reply` now carry `mode` (4th tuple element), viewer sorts newest-first, browser text reflects privacy model.
- `backend/bot/handlers/ghost_seen_v2.py` — browser uses `load_allowed_chats()`, `⚙ Manage` button → `_MANAGE_ID` panel with ON/OFF toggles per chat, `_toggle_permission_action`, `_send_plain_action` for send-without-reply, `_begin_input_for()` common input setup, `_viewer_buttons` shows message preview in Select labels, `_action_buttons` shows Reply + Send without reply only for single selection, `_open_chat_action`/`_actions_action`/`_begin_input_for` revalidate `is_chat_allowed()`.
- `backend/config.py` — `GHOST_SEEN_DESTINATION_CHAT_ID` (int) and `GHOST_SEEN_DESTINATION_CHAT_NAME` (str) loaded from env.
- `tests/test_52_ghost_seen_v2_stage1.py` — updated empty-state assertion for new privacy text.
- `tests/test_56_ghost_seen_v2_stage5.py` — updated for 4-tuple reply state, privacy prerequisites, new error messages.
- `tests/test_57_ghost_seen_v2_stage5_6.py` — 42 comprehensive hardening regression tests.

## Privacy model

- **Default**: all chats are NOT ALLOWED for Ghost Seen.
- **Enable**: the `⚙ Manage` panel in the Ghost Seen browser shows all private user dialogs with ON/OFF toggle buttons. Tapping OFF → ON calls `allow_chat(chat_id)`.
- **Disable**: tapping ON → OFF calls `disallow_chat(chat_id)`, which also clears any active selection and reply state for that chat.
- **Persistence**: allowed chat IDs are stored in the `bot_settings` Supabase table (key `ghost_seen_allowed_chats`, JSON-encoded list). When Supabase is unavailable, the in-memory set is used without persistence.
- **Source-chat identity**: privacy permission is keyed by the real Telegram source chat ID (integer). Never by display name, username, page, or message text.

## Message ordering

- `load_viewer_messages()` sorts by `message_id` descending (`reverse=True`).
- Page 1 always contains the newest messages; subsequent pages contain progressively older messages.
- Telethon's `iter_messages()` returns messages newest-first; the code no longer re-sorts them ascending.

## Selection UX

- Each Select button now shows a preview snippet of the message it targets (e.g., `Select ببین فردا میای؟`).
- When selected, the button changes to `✓ ببین فردا میای؟`.
- Selection identity remains the real Telegram `message_id` — never a row index or text.

## Reply modes

- **Reply** (`action:ghost_seen_v2_reply:<source>`): sends the owner's text as `reply_to=<selected_message_id>` via `send_reply()`.
- **Send without reply** (`action:ghost_seen_v2_send_plain:<source>`): sends the owner's text to the source chat without a reply target via `send_message_plain()`.
- Both modes require exactly one selected message, validate source-chat permission, consume state exactly once, and deliver to the source private chat.

## Destination configuration

```
GHOST_SEEN_DESTINATION_CHAT_ID=<numeric Telegram chat ID>
GHOST_SEEN_DESTINATION_CHAT_NAME=<user-defined display name>
```

- `GHOST_SEEN_DESTINATION_CHAT_ID`: read by `get_destination_chat_id()` in the service module. Numeric Telegram chat ID. Authoritative identity for future notification/Ghost Seen generated flows. Default: `0` (unconfigured).
- `GHOST_SEEN_DESTINATION_CHAT_NAME`: read by `get_destination_chat_name()`. Display-only label. Never used to locate or send messages. Default: `""` (empty).
- Both are loaded from environment variables via `os.getenv()` in the service module. Users must configure them in Render → Environment Variables.

## Security / state isolation

- Source chat ID vs panel chat ID: privacy validation, selection, and reply state are all keyed by source chat ID. Panel chat ID is only used as the session key and the pending-input chat. A stale callback from a disabled source chat is rejected by `is_chat_allowed()`.
- Reply state keyed by panel chat prevents cross-chat consumption.
- Every callback revalidates: source chat exists, is allowed, is a real private chat, selection cardinality is valid, and session identity matches.

## Validation

- Focused Ghost Seen v2 tests (Stages 1–5 + 5/6 hardening): **81 passed**
- Full Python suite: **887 passed, 23 skipped, 1 pre-existing warning**
- `compileall`: **PASS**
- `git diff --check`: **PASS**
- `bun tsc -b --noEmit`: **PASS**
- Exactly one `INVESTIGATION.md`: **confirmed**
- Telegram live E2E was **not** performed.

## Delivery

Commit and remote verification completed after this validation.
