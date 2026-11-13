# INVESTIGATION — History summary delivery through the Helper Bot's inline edit

_Base commit: `c18a2d5baeceefa66f064712bd36eba638b8faa5` (`main`, the
test-workspace deployment that produced the evidence). Working tree clean on
entry. No live Telegram, Render or Supabase credential exists in this
workspace; every claim below comes from the deployed source and the recorded
runtime evidence._

## 1. Symptom (confirmed production evidence)

History retrieval, the AI map/reduce calls and the final provider call all
completed. Result delivery then failed:

```
HISTORY_RESULT_DELIVERY_FAILED reason=destination_not_owner_private
HISTORY_RESULT_DELIVERY_FAILED reason=no_safe_text_path
```

The panel callback belonged to message id `48828` in source chat
`-1002750223875`. Both identifiers are evidence only — nothing in the code
hardcodes them. The chat is a group, not the owner's private chat.

## 2. What the History panel actually is

Source-verified, in this order:

1. `backend/helper/inline_engine.py::trigger` runs
   `self_client.inline_query(helper_username, query, entity=chat_id)` and sends
   the first result with `result.click(chat_id)`. The sender of the resulting
   message is therefore the **owner's own account**, and Telegram stamps the
   Helper Bot as its `via_bot`. The panel's inline-bot identity belongs to the
   Helper Bot.
2. `trigger` returns `msg.inline_message_id`; `PanelLifecycleManager.create_panel`
   stores it in the panel session (`session_manager.Session.inline_message_id`),
   so a later callback can be resolved by that string even without chat/message
   coordinates (`panels._resolve_session`).
3. The callback for such a message is delivered to the **Helper Bot** (the bot
   that owns the inline result), carrying `query.msg_id` as an
   `InputBotInlineMessageID`/`InputBotInlineMessageID64`. That object is the
   Telegram handle for `messages.editInlineBotMessage`.
4. `history._run_action` reads exactly that handle through
   `panel_edit.inline_message_id_from_event(event)` and passes it to
   `_run_summary` as `inline_id`.

The supported edit for this message type is therefore the Helper Bot's
`edit_message(InputBotInlineMessageID, text=..., buttons=...)`
(`messages.editInlineBotMessage`). The owner's account cannot edit an
inline-bot message through its chat/message coordinates; only the bot that
produced the inline result can edit it, and only through the inline handle.

## 3. Exact root cause of the recorded failure

`history._deliver_result` had two paths:

1. `await _paint(editor, body, buttons)` — the correct edit, but the editor was
   built with BOTH principals (`helper=get_client()`, `self_client=client`), so
   a refused inline edit silently degraded to a self-client chat edit that
   cannot work for an inline-bot message.
2. `_replace_with_text_result(...)` — a **replacement panel** delivered through
   `PanelLifecycleManager.replace_panel`, gated by
   `_owner_private_destination()`: the panel chat had to equal the owner's own
   private chat (Saved Messages). Any other chat — including the group in the
   evidence — was refused with `destination_not_owner_private`.

With Appearance preview active the History panel is a photo message, so its
text is a **caption** (1024 characters), while `MAX_SUMMARY_CHARS` is 2800 plus
metadata. `_deliver_result` therefore took the replacement path before even
attempting an edit, `_owner_private_destination` refused the group, and
`_run_summary` logged the terminal `no_safe_text_path`. The fallback also told
the owner to "run it again from your own Saved Messages panel", which the
requested behavior forbids.

Both observed log lines are fully explained by this path; no retrieval, AI,
provider, catalog or panel-creation failure is involved.

## 4. Telegram constraints that shape a correct fix

- A bot edits an inline result message only through
  `messages.editInlineBotMessage` with the inline message id. There is no
  lookup that can derive that handle from chat/message ids.
- A photo message stores its text as a caption with Telegram's 1024-character
  ceiling; a text message stores up to 4096. Telegram cannot convert a photo
  message into a text message in place, and changing media requires supplying
  new media — i.e. a different message, which the requested behavior forbids.
- Therefore: the summary must be edited into the SAME message. On a text panel
  the full 2800-character body fits. On a photo panel only a caption-sized
  result fits, so the delivered text is deterministically bounded with a visible
  marker instead of attempting an edit Telegram will refuse.

## 5. Corrective shape (what this change implements)

- One editor for every History panel edit: `ExistingPanelEditor` with the
  Helper Bot's inline handle, `self_client=None`. The owner's self-bot client is
  never a fallback panel editor.
- `_deliver_result` edits the original message. It uses
  `appearance_service.delivered_state(chat, msg)` only to choose the text
  bound (caption vs text); it never chooses a destination.
- No replacement, no second panel, no one-shot result token, no Saved Messages
  path, and no instruction to rerun from Saved Messages. All of that code was
  removed, not disabled behind a flag.
- A refused edit is classified from the Telegram exception TYPE
  (`permission`, `not_author`, `invalid_reference`, `caption_too_long`,
  `text_too_long`, `no_inline_panel_handle`, `telegram_error`) and logged as
  `HISTORY_RESULT_DELIVERY_FAILED reason=helper_panel_edit_failed`; a success is
  logged only after the Helper Bot's edit returned successfully. The failure
  notice is attempted on the SAME panel through the SAME editor, and creates no
  message anywhere else.
- Progress edits use the same Helper Bot-only editor, so the whole flow has one
  editing principal.

## 6. Deliberately unchanged

History retrieval (`history_service`), the map/reduce summarizer
(`history_ai_service`), ProviderManager/candidate behavior, the provider
prompt, the bounded chat picker, count/focus/link inputs, the panel registry and
navigation, `PanelLifecycleManager` semantics for other panels, Appearance
composition, and every other feature's legitimate use of Saved Messages. No new
client, listener, loop, scheduler, executor, supervisor, table, migration or
environment variable is introduced. No SQL was executed.

## 7. Residual risk and unverified points

- No live Telegram edit was observed from this workspace. The Helper Bot's
  authority is established from Telegram's inline-message ownership model and
  the existing `appearance.py`/`panel_edit.py` use of the same API, not from a
  live run.
- A photo panel's full 2800-character summary cannot fit a 1024-character
  caption. The fix preserves the message and delivers a bounded, visibly marked
  result; it does not claim the omitted tail was shown.
- If the callback carries no inline handle (an ordinary message panel rather
  than an inline result), no supported edit exists. That fails honestly with
  `kind=no_inline_panel_handle`; it does not redirect anywhere.
- A Helper Bot that is disconnected or not a member of the chat cannot edit;
  the error type is logged and the panel keeps its previous content.
