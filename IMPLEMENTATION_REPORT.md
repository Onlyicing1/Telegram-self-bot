# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Fixed the Ghost Seen single-message AI reply flow. The legacy "Type your instruction for the selected messages." prompt no longer appears when exactly one message is selected — the shared callback router now validates against the Ghost Seen **source chat** identity in addition to the panel callback chat.

No You.com, web search, provider, schema, frontend, or Render files were changed.

## Root Cause

The `_handle_input` cardinality guard in `backend/helper/panels.py` checked `count_selected(chat_id)` using only the **panel/callback chat ID** (the Ghost Room where the inline panel message lives). Ghost Seen selection state is keyed by the **source private chat ID** (tracked by `_current_panel_chat` in `ghost_seen.py`). These are different chat IDs in production — the panel is in the Ghost Room (e.g. `-10099999`) while selections are under the source private chat (e.g. `4242`).

When `count_selected(-10099999)` returned 0, the `== 1` guard never fired, allowing the stale `input:ghost_chat:ai_prompt` callback to arm `set_pending(...)` and render "Type your instruction for the selected messages."

## Files Changed

- `backend/helper/panels.py` — Extended the `_handle_input` guard for `ghost_chat:ai_prompt` to also check `current_chat_id()` (the Ghost Seen source chat) before the panel callback chat fallback.
- `tests/test_49_ghost_seen_flows.py` — Added `TestSourceChatIdentityGuard` with 3 regression tests covering panel/source chat mismatch, multi-select preservation, and count-0 behavior.
- `INVESTIGATION.md` — Updated with source-proven root cause, exact producer, and fix verification.
- `IMPLEMENTATION_REPORT.md` — This file.

## Callback path before fix

```
input:ghost_chat:ai_prompt callback
  → _handle_input("ghost_chat:ai_prompt", owner_id, chat_id=PANEL, ...)
  → guard: count_selected(PANEL)  // PANEL is Ghost Room, has 0 selections
  → guard does NOT fire (0 != 1)
  → get_input("ghost_chat", "ai_prompt") → found
  → set_pending(...) → _safe_edit("Type your instruction for the selected messages.")
```

## Callback path after fix

```
input:ghost_chat:ai_prompt callback
  → _handle_input("ghost_chat:ai_prompt", owner_id, chat_id=PANEL, ...)
  → guard: current_chat_id() = SOURCE  // source private chat
  → count_selected(SOURCE) == 1  → REJECT
  → clear_pending(owner_id), return   // no prompt armed
```

## Guard Logic

```python
source = current_chat_id()
if (source and count_selected(source) == 1) or count_selected(chat_id) == 1:
    # reject — single-selection cannot invoke the legacy prompt
```

This covers:
- Production case: source chat has 1 selection, panel chat differs
- Same-chat case: both source and panel match, either catches it
- Source-unknown fallback: panel chat checked directly when `current_chat_id` is 0

Multi-select (2+ in source) passes through as before. Count 0 in both also passes through — the renderer already restricts the button to `n_sel > 1`.

## Single-message AI behavior (unchanged)

The single-message AI route remains fixed-task execution:

```
select one incoming message
  → action:ghost_actions (target banner + AI Reply button)
  → action:ghost_ctx (context count: 1/5/10/20)
  → action:ghost_inform:yes|no (disclosure choice)
  → automatic AI generation via execute_ghost_seen_ai → Engine.execute
  → GHOST_ROOM_ID validation and send
```

No owner-written AI instruction. No `ai_prompt`. No `ai_reply_prompt`. Owner messages are conversation data, not control input.

## State (unchanged)

Ghost Seen selection, page, and pending AI reply state are in-memory dictionaries keyed by source chat ID. The `ghost_chats` Supabase table stores registry rows only (chat metadata, preview, unread count, timestamps). No selection or pending input state is persisted.

## Validation

- Focused Ghost Seen tests: `tests/test_49_ghost_seen_flows.py`, `tests/test_45_ghost_seen.py`, `tests/test_47_ghost_seen_entry.py` — **82 passed** (79 existing + 3 new regression)
- Full Python suite: **913 passed**, **1 pre-existing Starlette PendingDeprecationWarning**
- `.venv/bin/python -m compileall -q backend` — **PASS**
- `git diff --check` — **PASS**
- TypeScript check — **not needed** (no frontend changes)
- Repository-wide Investigation filename check: exactly `./INVESTIGATION.md`
- Telegram live E2E was **not performed** in this workspace
- No Render deployment performed

## Delivery

- Starting commit: `43ec469b75e2810650352de8ac7e12032ba671ca`
- Implementation commit: recorded after push below
- Push to `origin/main`: recorded after delivery below
- Remote HEAD verification: recorded after delivery below

## Final working-tree state

Will be verified clean after commit and push.