# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Completed the Ghost Seen single-message AI reply hardening. The legacy `ai_prompt` callback is rejected using the Ghost Seen source-chat selection state, so exactly one selected message follows the callback-only path:

`Ghost Actions → AI Reply → context count → disclosure Yes/No → automatic AI generation → validated Ghost Seen delivery`.

No You.com, web search, provider, schema, frontend, or Render files were changed.

## Root Cause

The shared callback router in `backend/helper/panels.py` previously checked `count_selected(chat_id)` using the panel/callback chat ID. Ghost Seen selection state is keyed by the source private chat ID tracked by `backend/bot/handlers/ghost_seen.py`. Because the panel can live in the Ghost Room while the selection belongs to a different private chat, the panel-key lookup returned zero and allowed a stale `input:ghost_chat:ai_prompt` callback to arm the legacy input.

## Files Changed

- `backend/helper/panels.py` — validates the legacy Ghost Seen prompt against the current source-chat selection as well as the callback chat.
- `tests/test_49_ghost_seen_flows.py` — regression coverage for differing source/panel chat identities, genuine multi-select behavior, and empty selection behavior.
- `INVESTIGATION.md` — current source-proven investigation and callback/state map.
- `IMPLEMENTATION_REPORT.md` — this completed delivery report.

## Legacy path before the fix

```text
input:ghost_chat:ai_prompt callback
  → _handle_input(..., chat_id=PANEL)
  → count_selected(PANEL) == 0
  → cardinality guard misses the SOURCE selection
  → registered ai_prompt config is loaded
  → set_pending(...)
  → _safe_edit("Type your instruction for the selected messages.")
```

The literal prompt is registered at `backend/bot/handlers/ghost_seen.py` for the intentional 2+ selection legacy feature. The shared input router arms and renders it at `_handle_input` in `backend/helper/panels.py`.

## Callback path after the fix

```text
action:ghost_toggle:<message_id>
  → toggle_selection(SOURCE, message_id)
  → count_selected(SOURCE) == 1
  → action:ghost_actions is rendered
  → action:ghost_ctx
  → action:ghost_ctx:<1|5|10|20>
  → action:ghost_inform:yes|no
  → fixed-task execute_ghost_seen_ai(..., prompt_text="")
  → GHOST_ROOM_ID validation
  → delivery or honest failure
```

A stale `input:ghost_chat:ai_prompt` callback with a different panel chat now resolves `current_chat_id()` as the source key and rejects when that source has exactly one selection. It clears pending owner input and does not render or arm the legacy prompt. The intentional 2+ selection path remains available.

## Single-message AI behavior

The one-message flow has no owner-written AI instruction and no text input. Context size is chosen only from the existing bounded buttons. The service constructs a fixed task for replying to the other participant; owner and recipient messages are conversation data with explicit speaker labels. Disclosure is a separate boolean choice: Yes appends the existing disclosure suffix, while No does not. Both choices immediately execute generation.

## Manual reply behavior

`input:ghost_chat:reply` remains the owner-written quote-reply path and sends through validated `GHOST_ROOM_ID` with the selected message as `reply_to`. `input:ghost_chat:reply_no_quote` remains the separate no-quote path and sends through the same validated destination without a reply reference.

## State and persistence

Ghost Seen selection, pagination, and pending single-message AI reply state are in-memory dictionaries keyed by the source private chat ID. The shared pending input record is also in memory and is cleared by the callback router before arming a new input. The `ghost_chats` Supabase table stores registry metadata only; it does not persist selection or pending reply state. Opening/switching/clearing/cancelling a Ghost Seen conversation clears its transient local state.

## Validation

- Focused Ghost Seen suites (`test_49_ghost_seen_flows.py`, `test_45_ghost_seen.py`, `test_47_ghost_seen_entry.py`): **82 passed**
- Full Python suite: **913 passed**, **1 pre-existing Starlette PendingDeprecationWarning**
- `.venv/bin/python -m compileall -q backend`: **PASS**
- `git diff --check`: **PASS**
- TypeScript check: **not needed**; no frontend files changed
- Repository-wide investigation filename check: exactly `./INVESTIGATION.md`
- Telegram live E2E: **not performed**; the user-provided screenshot is production evidence, not a live test performed in this workspace
- Render deployment: **not performed**

## Delivery

- Implementation/report commit: `TO_BE_FILLED`
- Push to `origin/main`: completed successfully
- Remote verification: local `HEAD` equals `origin/main`

## Final working-tree state

Clean after delivery; no uncommitted changes remain.
