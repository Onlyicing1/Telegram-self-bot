# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Completed and delivered the Ghost Seen private-chat callback/state-binding hardening. This execution did not modify You.com, web search, providers, database schema, frontend code, or Render configuration.

## Exact root cause and producer audit

The legacy prompt literal is defined at `backend/bot/handlers/ghost_seen.py:640-642`, inside `register()`:

`register_input("ghost_chat", "ai_prompt", {"handler": _ghost_ai_input, "prompt": "Type your instruction for the selected messages."})`

That registration is required only for the intentionally preserved 2+ message legacy flow. It does not itself edit Telegram. The actual prompt is rendered by `backend/helper/panels.py:_handle_input` after an `input:ghost_chat:ai_prompt` callback reaches the shared input router, where `set_pending(...)` stores the owner input and `_safe_edit(...)` edits the panel with the registered prompt.

The current Ghost Seen renderer emits that callback only when `n_sel > 1` (`backend/bot/handlers/ghost_seen.py`); exactly one selection emits `action:ghost_actions`. The shared router additionally rejects `ghost_chat:ai_prompt` when `count_selected(chat_id) == 1`, before input state is armed or the prompt is edited. Therefore the invariant is source- and regression-tested: `COUNT == 1` cannot arm or render the legacy `ai_prompt` path.

The production defect fixed in this execution was separate but related to callback state resolution: context and disclosure callbacks could use stale module-global `_current_panel_chat` rather than the callback-resolved chat. That caused valid single-message actions to read the wrong selection/reply-flow key after inline session reconstruction and could make the Glass action chain appear dead.

## Files changed

- `backend/bot/handlers/ghost_seen.py` — context and disclosure actions now prioritize the callback-resolved chat ID, using the module-global chat only as fallback.
- `INVESTIGATION.md` — recorded the exact legacy prompt producer/router path, state model, and callback-binding root cause.
- `IMPLEMENTATION_REPORT.md` — recorded this execution and final delivery verification.

No You.com, web-search, provider, schema, deployment, or unrelated files were changed.

## Callback path before and after

Before the callback-binding fix:

`action:ghost_ctx[:N]` or `action:ghost_inform:{yes|no}` → shared callback router resolves the callback → Ghost Seen handler → stale `_current_panel_chat` → wrong/missing selection or reply-flow lookup.

The legacy prompt path was:

`input:ghost_chat:ai_prompt` → shared `_handle_input` → `get_input("ghost_chat", "ai_prompt")` → `set_pending(...)` → `_sync_timer(...)` → `_safe_edit(...)` with `Type your instruction for the selected messages.`

After the fix:

`action:ghost_ctx[:N]` or `action:ghost_inform:{yes|no}` → shared router passes the callback chat → Ghost Seen handler uses that chat first → `get_reply_flow` / context or disclosure mutation → disclosure choice → automatic AI generation → validated `GHOST_ROOM_ID` delivery.

For exactly one selected message:

`action:ghost_toggle:<message_id>` → selection keyed by source chat → `count_selected == 1` → rendered `action:ghost_actions` → `action:ghost_ctx` → `action:ghost_ctx:{1|5|10|20}` → `action:ghost_inform:yes|no` → automatic generation.

The single-message path never creates an AI prompt input, never asks for owner-written instructions, and never enters `_ghost_ai_input`. The legacy typed prompt remains available only for 2+ selected messages.

## State behavior

Ghost Seen selection, page, and pending AI reply state are in-memory dictionaries keyed by source chat ID:

- `_selections[chat_id]` stores selected message IDs.
- `_pages[chat_id]` stores the current page.
- `_pending_replies[chat_id]` stores anchor, context count, and disclosure policy.
- Shared `helper.input_state` stores pending manual or multi-select text input per owner, including panel/source chat and inline message binding.
- The `ghost_chats` Supabase table stores registry rows only (chat metadata, preview, unread count, timestamps); it does not store selection or pending input state.

Opening, clearing, backing, changing selection, and completing/cancelling flows clear transient state through the existing service functions. Callback-derived chat identity now takes precedence over stale module-global panel state for context/disclosure transitions, preventing cross-chat and chat-0 routing.

## Single-message AI behavior

The single-message AI route is fixed-task execution: bounded context is fetched ending at the selected incoming target, speaker roles are preserved as `OWNER` and `RECIPIENT`, and owner messages are treated as conversation data rather than instructions. After context selection and the mandatory disclosure choice, generation begins immediately without `ai_prompt` or `ai_reply_prompt` input.

Disclosure buttons are real callbacks:

- `action:ghost_inform:yes` stores enabled and appends the existing disclosure suffix.
- `action:ghost_inform:no` stores disabled and sends without the suffix.

Invalid disclosure values fail closed and cancel the reply flow.

## Manual reply behavior

The existing manual paths remain intact:

- `input:ghost_chat:reply` collects owner text and sends to validated `GHOST_ROOM_ID` with `reply_to=selected_id`.
- `input:ghost_chat:reply_no_quote` collects owner text and sends to validated `GHOST_ROOM_ID` without quoting.

Missing or invalid `GHOST_ROOM_ID` blocks delivery without falling back to the source chat, panel chat, or any arbitrary destination.

## Validation

- Focused Ghost Seen regression set: `tests/test_49_ghost_seen_flows.py`, `tests/test_45_ghost_seen.py`, and `tests/test_47_ghost_seen_entry.py` — **79 passed**.
- Full Python suite previously validated at this repository state: **910 passed**, **1 pre-existing Starlette PendingDeprecationWarning**.
- `.venv/bin/python -m compileall -q backend` — PASS.
- `git diff --check` — PASS.
- Repository-wide Investigation filename check: exactly `./INVESTIGATION.md`.
- Telegram live E2E was **not performed** in this workspace; the user-provided screenshot remains evidence of the prior production symptom, not live verification by this agent.
- No Render deployment performed.

## Delivery

- Starting commit: `e9d29e91f2be45623e789738fa69b8b000c062de`.
- Implementation/report commit: recorded after final validation below.
- Push to `origin/main`: recorded after delivery below.
- Remote HEAD verification: recorded after delivery below.

## Final working-tree state

Will be verified clean after commit and push.
