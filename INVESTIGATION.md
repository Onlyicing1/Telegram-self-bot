# INVESTIGATION

## Problem

The current Ghost Seen source has two separate reply surfaces:

1. Single-message selection is intended to use a Glass action flow, but a stale inline callback can still invoke the registered legacy `ghost_chat:ai_prompt` input. The shared callback router creates a pending text-input state before any Ghost Seen action handler runs, producing the forbidden “Type your instruction for the selected messages.”
2. Manual reply buttons are real input callbacks, but delivery uses the authoritative `GHOST_ROOM_ID`; it must be verified and fail closed rather than assuming the source conversation is the destination.

The repository is authoritative. No You.com, web-search, provider, schema, or unrelated system is in scope.

## Complete Current Flow

```text
Ghost Seen panel
  → select a private-chat message (action:ghost_toggle:<message_id>)
  → handler toggles per-chat selection state and re-renders ghost_chat
  → exactly one selection renders action:ghost_actions
     2+ selections render input:ghost_chat:ai_prompt (legacy multi-select only)
  → single-message action menu resolves the anchor and renders:
       input:ghost_chat:reply
       input:ghost_chat:reply_no_quote
       action:ghost_ctx
  → manual path arms a text input and sends to GHOST_ROOM_ID
  → AI path selects bounded context count
  → disclosure yes/no callback
  → automatic fixed AI task through execute_ghost_seen_ai → Engine.execute
  → optional disclosure suffix
  → GHOST_ROOM_ID validation
  → send_message(destination)
```

## Stage-by-Stage Explanation

### 1. Ghost Seen chat list

- User sees `👻 Ghost Seen` and rows sourced from the `ghost_chats` registry.
- `panel:ghost_seen` reaches `_ghost_list_panel_handler`.
- Each row emits `action:ghost_open:<chat_id>`.
- Opening sets the current panel chat, resets page 0, clears registry unread state, and renders the message page.
- `ghost_chats` is source-chat registry state, not a delivery destination.
- This stage is active and necessary for selecting a conversation.

### 2. Message page and selection

- User sees up to five messages with explicit direction labels and selection marks.
- Each message button emits `action:ghost_toggle:<message_id>`.
- `_ghost_toggle_action` uses the current chat and `toggle_selection(chat_id, msg_id)`, then re-renders `ghost_chat`.
- Selection state is `_selections[chat_id]`, capped at ten IDs.
- `count_selected(chat_id)` decides the next control:
  - exactly one: `⚡ Reply / Actions` → `action:ghost_actions`
  - two or more: `🤖 AI on selection` → `input:ghost_chat:ai_prompt`
- The branch is active and is the intended single-vs-multi distinction.

### 3. Single-message action menu

- `_ghost_actions_action` requires exactly one selected ID and a configured self client.
- It resolves the anchor message, starts `_pending_replies[chat_id]` with `anchor`, `context_n=None`, and `informed=None`, and renders the reply-target banner.
- Buttons are real Glass callbacks:
  - `input:ghost_chat:reply` — manual quote reply
  - `input:ghost_chat:reply_no_quote` — manual no-quote reply
  - `action:ghost_ctx` — AI context policy menu
  - `action:ghost_back` — return to list
- This stage is active and required. The target banner identifies the selected message.

### 4. Manual reply input

- The two manual buttons are registered in `ghost_seen.register` and routed by the shared `input:` branch.
- `_handle_input` stores a per-owner pending input with the registered handler, panel chat, panel message, prompt, and timeout, then edits the panel to show the input prompt.
- The next owner message is delivered to `_ghost_reply_input` or `_ghost_reply_no_quote_input`.
- Both resolve `_resolve_ghost_destination()` from `GHOST_ROOM_ID`; neither uses `ghost_chats` or the current source chat as fallback.
- Quote mode calls `send_message(dst, text, reply_to=selected_id)`; no-quote mode calls `send_message(dst, text)`.
- Both clear selection after processing; failures are logged and do not claim success.
- This stage is active. The manual input behavior is legitimate and distinct from the forbidden AI instruction input.

### 5. AI context policy

- `action:ghost_ctx` reaches `_ghost_ctx_action`.
- With no argument, it renders buttons for 1, 5, 10, or 20 messages, each using `action:ghost_ctx:<n>`.
- The service validates the allow-list and stores `context_n` in the pending flow.
- Choosing a valid count renders exactly two disclosure buttons. No text input is armed.
- This stage controls context amount and is necessary; it is not an AI instruction.

### 6. Disclosure

- `action:ghost_inform:yes` or `action:ghost_inform:no` reaches `_ghost_inform_action`.
- It validates and stores the boolean disclosure policy, then immediately calls `_execute_single_ghost_ai_reply`.
- Invalid values cancel the flow and render an error.
- Disclosure is delivery policy, not model instruction.

### 7. Automatic AI generation and delivery

- `_execute_single_ghost_ai_reply` consumes the complete flow exactly once.
- It validates `GHOST_ROOM_ID` before delivery, fetches the bounded context ending at the anchor, and rejects an outgoing anchor because the target must be a recipient message.
- `execute_ghost_seen_ai` builds explicit `OWNER` / `RECIPIENT` lines, marks the final recipient message as the target in its fixed task, and invokes the existing `Engine.execute(AIRequest)` path with an empty prompt string.
- On success, the handler appends `AI_DISCLOSURE_SUFFIX` only when disclosure is enabled and sends to the validated `GHOST_ROOM_ID`.
- Missing/invalid destination, failed context fetch, invalid target direction, AI failure, and send failure all fail closed.

### 8. Back and Cancel

- `action:ghost_back` returns to the list; shared panel navigation clears pending owner input when appropriate.
- `action:ghost_clear` clears selection and re-renders the chat.
- Input pages render a `panel:ghost_chat` cancel button; the shared panel route clears pending input and returns to the chat page.
- The service `cancel_reply_flow` and `reset_chat_state` clear transient reply state.

## Manual Reply Flow

```text
select one message
  → action:ghost_actions
  → input:ghost_chat:reply OR input:ghost_chat:reply_no_quote
  → shared _handle_input stores pending owner input
  → owner types reply
  → _ghost_reply_input / _ghost_reply_no_quote_input
  → GHOST_ROOM_ID validation
  → send_message(dst, text, reply_to=anchor) OR send_message(dst, text)
  → clear selection
```

The manual flow does not use the AI engine. It is active, callback-registered, and has separate quote/no-quote semantics. The implementation must preserve it while ensuring its destination remains fail closed.

## AI Reply Flow

```text
select exactly one incoming message
  → action:ghost_actions with target banner
  → action:ghost_ctx
  → action:ghost_ctx:<1|5|10|20>
  → action:ghost_inform:yes OR action:ghost_inform:no
  → consume complete reply flow
  → bounded fetch_context_window
  → fixed task: generate owner's natural reply to recipient
  → Engine.execute(AIRequest), no owner prompt
  → optional suffix
  → GHOST_ROOM_ID validation and send
```

The current fixed task constructs `OWNER` and `RECIPIENT` speaker labels. Owner messages are context data, not control input. The selected incoming message is the final target. No `ai_reply_prompt` registration exists in the current handler.

## Callback Map

| Callback | Receiver | Result |
|---|---|---|
| `action:ghost_open:<id>` | `_ghost_open_action` | Open source private chat |
| `action:ghost_toggle:<id>` | `_ghost_toggle_action` | Toggle selection and re-render |
| `action:ghost_page:prev/next` | `_ghost_page_action` | Change page |
| `action:ghost_clear` | `_ghost_clear_action` | Clear selection |
| `action:ghost_actions` | `_ghost_actions_action` | Target banner and manual/AI choices |
| `action:ghost_ctx` | `_ghost_ctx_action` | Context-size menu |
| `action:ghost_ctx:<n>` | `_ghost_ctx_action` | Store context count and show disclosure |
| `action:ghost_inform:yes/no` | `_ghost_inform_action` | Store policy and execute automatically |
| `action:ghost_remove` | `_ghost_remove_action` | Remove registry row/local state |
| `action:ghost_back` | `_ghost_back_action` | Return to chat list |
| `input:ghost_chat:reply` | `_ghost_reply_input` | Typed quote reply |
| `input:ghost_chat:reply_no_quote` | `_ghost_reply_no_quote_input` | Typed no-quote reply |
| `input:ghost_chat:ai_prompt` | `_ghost_ai_input` | Legacy multi-select typed AI path; must be blocked for one selection |

All callbacks enter the single shared router in `backend/helper/panels.py`, which resolves the panel session and dispatches by `panel:`, `action:`, or `input:` prefix.

## State Map

- `_current_panel_chat` in `ghost_seen.py`: current source chat used by panel actions.
- `_selections: dict[int, set[int]]`: selected source-message IDs, keyed by source chat.
- `_pages: dict[int, int]`: current message page per source chat.
- `_pending_replies: dict[int, dict]`: `{anchor, context_n, informed}` keyed by source chat; consumed once after disclosure.
- Shared `_pending` in `helper/input_state.py`: per-owner manual or legacy text-input state, including panel/chat/message binding and timeout.
- Lifecycle session state in `helper/session_manager.py`: panel message/session lookup used by callback routing.

## Confirmed Problems

1. The shared `_handle_input` route accepted `input:ghost_chat:ai_prompt` without checking selection cardinality. A stale inline button could therefore show the legacy instruction prompt even when one message was selected. This is source-proven.
2. The legacy `ai_prompt` registration remains necessary for 2+ selections, so removing it globally would break the separate multi-select flow. The guard is single-selection-specific.
3. Ghost Seen action handlers historically preferred the module-global `_current_panel_chat`; the callback router's authoritative resolved chat was not used consistently. After an inline session/callback rebinding, this could read the wrong selection/reply-flow key and make valid buttons appear unresponsive. The corrected path uses the callback chat ID first for context/disclosure transitions.
4. `_ghost_actions_action` and its buttons are registered in the current handler; the source does not show a duplicate Ghost Seen action registration. The observed missing buttons are consistent with stale callback state bypassing this action menu, not with absent button construction.

## Likely Problems

- A previously rendered inline panel or stale callback payload is likely surviving across a runtime/session update and invoking `input:ghost_chat:ai_prompt` directly. The shared router’s lack of a cardinality guard makes this possible.
- Manual reply “does nothing” may be a stale input panel/session or an unresolved destination; source-level delivery is present and must be covered with router/state tests.

## Unknowns

- The source cannot prove which exact stale Telegram inline message the owner pressed or whether Telegram cached an older panel payload.
- Actual Telegram API delivery, Telegram client-side button rendering, and current Render process state cannot be verified in this repository-only environment.
- Whether the configured `GHOST_ROOM_ID` chat accepts `reply_to` for a source-chat message ID depends on Telegram semantics/runtime; the existing architecture treats the configured Ghost Room as the delivery destination and must not guess another destination.

## Required Final Behavior

For exactly one selected incoming private message:

```text
selection
  → real rendered Ghost Seen action menu with target banner
  → Reply myself quote/no-quote OR Reply with AI
  → optional legitimate bounded context count
  → exactly two disclosure buttons
  → immediate automatic AI generation
  → OWNER/RECIPIENT role-aware context
  → optional suffix
  → validated GHOST_ROOM_ID delivery
```

No single-message path may show or arm `ai_prompt`, `ai_reply_prompt`, “Type your instruction,” or any owner-written AI instruction. Two or more selections may retain the legacy typed multi-select path. Any missing/invalid destination or failed context must fail closed.

## Implementation Plan

1. Keep the shared `input:` cardinality guard: `ghost_chat:ai_prompt` is rejected when exactly one message is selected, while 2+ selections retain the typed legacy flow.
2. Resolve Ghost Seen context/disclosure state from the callback's authoritative chat ID before the module-global fallback, preventing cross-chat/stale-session routing.
3. Add focused tests for rendered one-selection buttons, callback routing/state binding, manual quote/no-quote delivery, automatic AI flow without prompt, disclosure policy, role-aware bounded context, and multi-select preservation.
4. Run focused Ghost Seen tests, the full suite, `compileall`, and `git diff --check`; review for duplicate callbacks and stale single-message prompt paths.
5. Update the implementation report accurately, commit only Ghost Seen/investigation/report files, push `origin/main`, and verify local/remote parity and a clean tree.
