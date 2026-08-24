# Ghost Seen AI Reply Removal Investigation

## Decision

Ghost Seen AI Reply was removed rather than patched again. Repeated Telegram evidence showed that the owner-prompt experience was not acceptable, so the entire Ghost Seen AI surface and its legacy owner-input producer were deleted. This document is the sole investigation document.

## Former producer and path

The former producer was the `register_input("ghost_chat", "ai_prompt", ...)` call in `backend/bot/handlers/ghost_seen.py`. Its prompt value was `Type your instruction for the selected messages.`. The callback was routed by the shared panel input router into the registered `_ghost_ai_input` handler, with pending input stored by the generic input-state system. The old path also included `ghost_actions`, `ghost_ctx`, `ghost_inform`, pending reply-flow state, context fetching, AI task construction, engine execution, disclosure suffix handling, and Ghost Room delivery.

The prior callback-binding/cardinality fixes did not remove this producer; they only attempted to prevent selected single-message state from reaching it. That left a live legacy input registration and a stale Telegram callback surface. The clean resolution was to delete the Ghost Seen AI producer, callbacks, handler, service state, and execution path instead of adding another guard.

## Removal performed

- `backend/bot/handlers/ghost_seen.py`: removed the AI Reply action, context/disclosure actions, automatic AI execution/delivery, and `ai_prompt` input registration/handler.
- `backend/services/ghost_seen_service.py`: removed AI pending state, context/disclosure state transitions, context-window fetching used only by AI, AI prompt construction, disclosure suffix, and `execute_ghost_seen_ai`.
- `backend/helper/panels.py`: removed the Ghost Seen-specific legacy AI-input guard because the input no longer exists.
- Tests were updated so removed AI-only expectations are skipped as historical coverage, while registration tests assert `ghost_ctx`, `ghost_inform`, and `ai_prompt` are absent.

## Current Ghost Seen path

Ghost Seen opens through the existing panel registration, renders registry chats, fetches passive message pages, and maintains selection/page state in the existing service dictionaries. A one-message selection reaches the preserved `ghost_actions` menu, which now contains only manual quote and no-quote reply inputs plus navigation. There is no AI button and no AI callback chain.

Manual reply remains:

`ghost_actions` → `input:ghost_chat:reply` or `input:ghost_chat:reply_no_quote` → existing input router → manual reply handler → validated `GHOST_ROOM_ID` → Telegram send.

The quote variant uses the selected message ID as `reply_to`; the no-quote variant sends without `reply_to`. Missing or invalid `GHOST_ROOM_ID` fails closed.

## State and persistence

The `ghost_chats` Supabase table stores registry metadata only. Ghost Seen selection and pagination are in-memory dictionaries in `ghost_seen_service.py`. The former Ghost Seen AI pending state was also in-memory and has been deleted. No Firebase/Firestore persistence is used for this feature, and no AI-specific state remains to be restored or consumed. Registry metadata is never used as a delivery destination.

## Verification

Repository-wide production search found no Ghost Seen `ai_prompt`, `ai_reply_prompt`, `ghost_ctx`, `ghost_inform`, `_ghost_ai_input`, `_pending_replies`, `execute_ghost_seen_ai`, AI disclosure suffix, or fixed Ghost Seen AI task. The remaining `ghost_actions` references are the preserved manual Reply / Actions menu. Exactly one investigation file exists: `./INVESTIGATION.md`.

Focused Ghost Seen suites passed with **70 passed and 42 skipped**; skipped tests are historical tests whose assertions exclusively require the deleted AI feature. The full suite passed with **870 passed, 43 skipped, and one pre-existing warning**. `compileall` and `git diff --check` passed.

Telegram live E2E was not performed. The user-provided Telegram screenshot was not treated as a live test by this execution.
