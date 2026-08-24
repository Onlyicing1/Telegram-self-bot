# Implementation Report — Ghost Seen v2 Stage 5

## Scope

Implemented the real manual Reply flow on top of Stages 1–4. The path is:

Message Viewer → select exactly one message → Action Menu → Reply → owner types reply text → the exact text is sent to the validated SOURCE private chat as a reply (`reply_to=<selected message ID>`) → honest success/failure status → clean state.

AI Reply remains an inert Stage 4 placeholder. No AI generation, prompt, context selection, disclosure, provider call, web search, or You.com usage was added. No legacy Ghost Seen code was resurrected.

## Files changed

- `backend/services/ghost_seen_v2.py` — `reply_target()`, panel-chat-keyed reply state (`begin_reply` / `get_reply` / `consume_reply` / `clear_reply` / `clear_all_replies`), and `send_reply()` delegating to the existing `backend.telegram_api.messages.send_message` typed wrapper with `reply_to`.
- `backend/bot/handlers/ghost_seen_v2.py` — Action Menu Reply now routes to `action:ghost_seen_v2_reply:<source>`; `_reply_action` (strict one-selection validation + input arming), `_ghost_reply_input_handler` (validated consumption + Telegram delivery + honest result), `_reply_cancel_action` (clears pending input and reply state, returns to Action Menu/viewer), reply-state cleanup on Actions-Back and on opening another chat.
- `tests/test_56_ghost_seen_v2_stage5.py` — focused Stage 5 regression tests.
- `INVESTIGATION.md` — Stage 5 state/callback tracing appended (still the only investigation file).
- `IMPLEMENTATION_REPORT.md` — this report.

## Exact Reply flow

1. Action Menu (`Reply`) → `ghost_seen_v2_reply:<source>`.
2. `reply_target(source)` must return exactly one selected Telegram message ID; 0 or 2+ fail closed (never arm input).
3. The viewer session must resolve to the same source chat; otherwise fail closed.
4. `begin_reply(panel_chat, source, message_id, panel_msg_id)` arms the reply state in-memory, keyed by the PANEL chat (the only chat where the owner's typed reply is consumed).
5. The shared pending-input system (`set_pending`, owner-keyed) asks the owner for the reply text; the input listener only consumes an outgoing message in that exact panel chat.
6. On the owner's message: `consume_reply(panel_chat)` atomically takes the state, re-validates that the source still has exactly that one message selected, then sends the exact text unchanged via the existing Telegram send utility with `reply_to=<message_id>`.
7. Success clears selection + reply state and shows `✅ Reply sent.`; failure consumes the state (never double-sends) and shows an honest `❌ Reply failed: ...`.
8. Cancel and Actions-Back clear pending input and reply state without sending; opening another chat clears the old reply state.

## Source-chat identity handling

The destination is always the validated Stage 2/3 source private chat from the callback/session state. The panel chat is used only as: (a) the session key, (b) where the typed reply is consumed, and (c) the reply state key. It is never passed to Telethon as the delivery target. Display names, pages, usernames, and row indexes are never used as identities.

## Selected-message-ID handling

The target is the real Telegram `message_id` captured at selection time and re-validated against the current one-message selection before sending. `send_reply` passes `reply_to=int(message_id)`.

## Input-state behavior

Uses the repository's existing generic pending-input infrastructure (`backend/helper/input_state.py` + the chat-scoped input listener in `backend/helper/inline_sender.py`). The only new state is the panel-chat-keyed in-memory reply record in the v2 service module; it is not a new dispatcher or callback architecture.

## Telegram delivery

`backend.telegram_api.messages.send_message(self_client, source_chat_id, text, reply_to=message_id)` — the existing typed Telegram send utility. No second client, no Bot API, no Saved Messages, no GHOST_ROOM_ID, no source-chat fallback. The owner's text is sent exactly as entered; it is never interpreted as an instruction.

## Confirmations

- AI Reply remains unimplemented (inert Stage 4 placeholder, "Coming in the next stage.").
- Stages 1–4 behavior remains intact (all Stage 1–4 tests still pass).
- No legacy `ai_prompt`/`input:ghost_chat:ai_prompt`/`ghost_actions`/`ghost_ctx`/`ghost_inform`/`GHOST_ROOM_ID` exists in v2 production source (source-verified).
- Exactly one `INVESTIGATION.md` remains.

## Validation

- Focused Stage 1–5 tests: **32 passed**.
- Full Python suite: **838 passed, 23 skipped, 1 pre-existing warning**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.
- Telegram live E2E was **not** performed (the user-provided screenshots are evidence of production behavior only).

## Delivery

Commit and remote verification completed after this validation.