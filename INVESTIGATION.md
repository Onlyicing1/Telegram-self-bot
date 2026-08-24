# INVESTIGATION

## Problem

When exactly ONE Ghost Seen private-chat message is selected, Telegram still shows:

"Type your instruction for the selected messages."

instead of the intended Ghost Actions menu with real Glass buttons.

The user-provided Telegram screenshots prove this is a real production behavior.

## Root Cause (source-proven)

The shared callback router (`backend/helper/panels.py:_handle_input`) contains a cardinality guard at lines 526-533. The previous version of the guard checked:

```python
if count_selected(chat_id) == 1:
    # reject
```

where `chat_id` is the **panel/callback chat** (resolved from the lifecycle session). In real Telegram usage, the Ghost Seen panel is rendered inside the configured **Ghost Room** (a group/channel with negative ID from `GHOST_ROOM_ID`), while Ghost Seen selection state is keyed by the **source private chat** (a positive integer tracked by the `_current_panel_chat` module-global in `ghost_seen.py`).

These are fundamentally different chat IDs:

- Source private chat: e.g. `4242` (positive, one-to-one conversation)
- Panel/Ghost Room: e.g. `-10099999` (negative, group where the inline panel lives)

When `count_selected(chat_id)` uses the panel chat ID, it sees **0 selections** (because selections are stored under the source chat key). The `== 1` guard never fires, the callback falls through to `get_input("ghost_chat", "ai_prompt")`, and `set_pending(...)` arms the legacy text input with `_safe_edit(...)` rendering:

"Type your instruction for the selected messages."

This is the exact producer of the forbidden UI.

### Why previous fixes missed it

| Attempt | What it did | Why it failed |
|---|---|---|
| Router cardinality guard (`count == 1` reject) | Checked `count_selected(chat_id)` using only the panel chat | Panel chat key has 0 selections; guard never matched |
| Callback-binding hardening (`chat_id or _current_chat()`) | Fixed context/disclosure actions to prefer the callback chat | Did not fix the `_handle_input` guard which was the actual entry point for the legacy prompt |
| Ghost Actions button building | Correctly builds `action:ghost_actions` for count==1 | The stale ai_prompt callback bypasses Ghost Actions entirely — it enters through `_handle_input` directly |

### Exact producer

1. **Literal:** `register_input("ghost_chat", "ai_prompt", {"handler": _ghost_ai_input, "prompt": "Type your instruction for the selected messages."})` at `backend/bot/handlers/ghost_seen.py:640-642`
2. **Arming:** `backend/helper/panels.py:_handle_input` lines 546-555: `set_pending(owner_id, panel_id, handler, chat_id or 0, prompt, ...)`
3. **Rendering:** `_safe_edit(event, styled_prompt, built, chat_id, msg_id)` at line 574 of `panels.py`
4. **Entry:** The `input:ghost_chat:ai_prompt` callback enters the shared router via `_handle_input` (line 522). The guard at 526-533 was keyed against the wrong chat identity.

## Complete Current Flow

```text
action:ghost_open:<source_chat_id>  (e.g. 4242)
  → _set_current_chat(4242)          // module-global
  → render ghost_chat panel in       // panel lives in Ghost Room (-10099999)
    the Ghost Room

action:ghost_toggle:<message_id>
  → toggle_selection(source_chat_id=4242, msg_id)
  → count_selected(4242) == 1
  → render action:ghost_actions button

Stale callback: input:ghost_chat:ai_prompt
  → _handle_input(event, "ghost_chat:ai_prompt", owner_id, chat_id=-10099999, msg_id=...)
  → guard: count_selected(chat_id=-10099999) == 0   ← WRONG KEY
  → guard does NOT fire (0 != 1)
  → get_input("ghost_chat", "ai_prompt") → found
  → set_pending(...) → _safe_edit("Type your instruction...")
```

## Fixed Flow

```text
Stale callback: input:ghost_chat:ai_prompt
  → _handle_input(event, "ghost_chat:ai_prompt", owner_id, chat_id=-10099999, msg_id=...)
  → guard: current_chat_id() = 4242  (source chat)
  → count_selected(4242) == 1  → REJECT
  → clear_pending, return (no prompt armed)
```

The guard now checks **both** the Ghost Seen source chat (via `current_chat_id()`) and the panel callback chat:

```python
source = current_chat_id()
if (source and count_selected(source) == 1) or count_selected(chat_id) == 1:
    # reject
```

This correctly catches:
- Source chat has 1 selection, panel chat differs (production case)
- Source chat has 1 selection, panel chat matches (same-chat case)
- Panel chat has 1 selection directly (fallback for when `current_chat_id` is 0)

Multi-select (2+ in source) passes through as before. Count 0 in both also passes through (the renderer already restricts the button to `n_sel > 1`).

## Callback Map (unchanged)

| Callback | Receiver | Result |
|---|---|---|
| `action:ghost_open:<id>` | `_ghost_open_action` | Open source private chat |
| `action:ghost_toggle:<id>` | `_ghost_toggle_action` | Toggle selection and re-render |
| `action:ghost_actions` | `_ghost_actions_action` | Target banner and manual/AI choices |
| `action:ghost_ctx` | `_ghost_ctx_action` | Context-size menu |
| `action:ghost_ctx:<n>` | `_ghost_ctx_action` | Store context count and show disclosure |
| `action:ghost_inform:yes/no` | `_ghost_inform_action` | Store policy and execute automatically |
| `input:ghost_chat:reply` | `_ghost_reply_input` | Typed quote reply |
| `input:ghost_chat:reply_no_quote` | `_ghost_reply_no_quote_input` | Typed no-quote reply |
| `input:ghost_chat:ai_prompt` | `_ghost_ai_input` | Legacy multi-select (NOW GUARDED BY SOURCE CHAT) |

## State Map (unchanged)

- `_current_panel_chat` in `ghost_seen.py`: current source chat used by panel actions
- `_selections: dict[int, set[int]]`: selected source-message IDs, keyed by source chat
- `_pending_replies: dict[int, dict]`: `{anchor, context_n, informed}` keyed by source chat
- Shared `_pending` in `helper/input_state.py`: per-owner manual or multi-select text-input state
- `ghost_chats` Supabase table: registry rows only (not selection or pending state)

## One-Message Invariant (now enforced)

For exactly ONE selected Ghost Seen message:

```
selection → Ghost Actions → AI Reply → Context Count → Disclosure Yes/No → automatic AI generation → GHOST_ROOM_ID delivery
```

The single-message path never arms `input:ghost_chat:ai_prompt`.

## Tests

- `TestSourceChatIdentityGuard` (3 tests): source/panel chat identity mismatch, multi-select preservation, count-0 behavior
- Existing focused tests: 82 passed (including all previous regressions)
- Full suite: 913 passed, 1 pre-existing Starlette warning

## Validation Limitations

- Telegram live E2E was not performed in this workspace
- The user-provided Telegram screenshots remain evidence of the prior production symptom
- No Render deployment performed
- No You.com, web search, provider, schema, or unrelated system changes