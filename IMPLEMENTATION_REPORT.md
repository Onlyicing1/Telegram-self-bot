# IMPLEMENTATION REPORT — History summary delivered by editing the original panel

_Base commit: `c18a2d5baeceefa66f064712bd36eba638b8faa5` (`main`, the
test-workspace deployment carrying the failure). This document replaces all
earlier reports; only the current verified state is described._

## 1. Root cause (verified)

The History workflow completed retrieval and AI generation; delivery failed
with `destination_not_owner_private` and then `no_safe_text_path`.

`backend/bot/handlers/history.py::_deliver_result` never reached a supported
edit when the panel was a photo message with a long summary:

- with Appearance preview active the panel is delivered as an **inline photo
  result**, so its text is a caption (1024 characters) while the summary body
  can reach 2800 plus metadata;
- the code therefore called `_replace_with_text_result`, which creates a
  **replacement panel** through `PanelLifecycleManager.replace_panel`;
- that replacement was gated by `_owner_private_destination()` — allowed only
  when the panel chat equals the owner's own private chat. The recorded panel
  source chat `-1002750223875` is a group, so it was refused with
  `destination_not_owner_private`, and the operation ended with
  `no_safe_text_path`.

The same function's normal edit path built `ExistingPanelEditor` with BOTH the
Helper Bot and the owner's self-bot client. For an inline-bot message the
self-client edit is invalid (only the bot that sent the inline result may edit
it), so that fallback could only hide the real failure. The failure text also
instructed the owner to rerun from a Saved Messages panel, which is prohibited.

## 2. Helper Bot ownership and editing mechanism

The History panel is an inline result: `inline_engine.trigger` runs
`self_client.inline_query(helper, query)` and sends `result.click(chat)`, so the
message is sent by the owner's account with the Helper Bot as `via_bot` and its
inline-bot identity belongs to the Helper Bot. The callback arrives at the
Helper Bot with `query.msg_id` as an `InputBotInlineMessageID` /
`InputBotInlineMessageID64`.

`history._editor` now builds `ExistingPanelEditor` with `helper=get_client()`
and **`self_client=None`**, and `_run_action`/input handlers pass the inline
handle obtained from the callback (`inline_message_id_from_event`). The edit is
`messages.editInlineBotMessage` via Telethon's
`edit_message(InputBotInlineMessageID, text=..., buttons=...)` — the same
mechanism already used by `backend/bot/handlers/appearance.py` and the Save
progress path. The owner's self-bot client is never supplied as an editor, and
no second client is created.

## 3. Files changed

| File | Change |
|---|---|
| `backend/bot/handlers/history.py` | Helper-Bot-only `_editor`; delivery edits the original panel; caption-aware text bound; Telegram-error classification; removed the replacement/Saved-Messages delivery path, result-view tokens and the "rerun from Saved Messages" notice |
| `backend/helper/panel_edit.py` | `ExistingPanelEditor.last_error` records the refused exception TYPE (no behavior change for successful edits; stale generations are labelled) |
| `tests/test_history_panel.py` | Replaced delivery tests: Helper Bot ownership, in-place edit, caption/text bounds, permission and invalid-reference classification, missing-handle behavior, no replacement, no Saved Messages, no self-client edit; existing retrieval/AI/link tests kept |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | Replaced in full with this verified state |
| `ROADMAP.md` | P1 delivery record corrected to the Helper-Bot inline-edit approach |

Not changed: `history_service`, `history_ai_service`, ProviderManager/candidate
behavior, prompts, panel lifecycle for other features, Appearance composition,
other Saved Messages flows, database/schema, Render configuration.
`DATABASE_ARCHITECTURE.md` was not modified and no SQL was executed.

## 4. Implemented behavior

- **Edits the same message.** The summary is rendered into the original History
  panel through the Helper Bot's inline edit. Message id, chat, `via_bot`
  identity and the panel's keyboard/navigation context are preserved.
- **Error-free short results.** A text panel carries the complete result up to
  Telegram's 4096-character text limit; a photo panel carries a caption up to
  1024.
- **Long results.** On a media (photo) panel the text is deterministically
  bounded to the caption limit with the visible marker
  `… (bounded to this panel's Telegram limit)`, so the edit is valid and the
  original message survives. On a text panel the full 2800-character summary
  fits unchanged.
- **No replacement, no redirection.** `_replace_with_text_result`,
  `_owner_private_destination`, the one-shot `_RESULT_VIEWS` tokens, the
  `replace_panel` call and the Saved Messages instruction were removed from the
  History module. No new panel, message or chat is created in any failure path.
- **Honest failure.** A refused edit is classified from the Telegram error type
  (`permission`, `not_author`, `invalid_reference`, `caption_too_long`,
  `text_too_long`, `no_inline_panel_handle`, `telegram_error`), logged as
  `HISTORY_RESULT_DELIVERY_FAILED reason=helper_panel_edit_failed error=… kind=…`,
  and never logged as `HISTORY_RESULT_DELIVERED`. The failure notice is
  attempted on the same panel through the same Helper Bot editor; if that is
  also refused, nothing is sent anywhere.
- **One editing principal.** Progress, link, focus and result paints all use
  the Helper Bot editor; the self-bot client is used only for non-panel
  operations (deleting the owner's input message).

## 5. Automated test results

| Check | Command | Result |
|---|---|---|
| Focused History panel suite | `pytest tests/test_history_panel.py -q` | **42 passed** |
| Panel/lifecycle/callback/edit regressions | `pytest tests -q -k "history or panel or edit or callback or lifecycle or save_success or save_reply or taskloom"` | **881 passed, 3 skipped** |
| Full suite | `pytest tests -q` | **7587 passed, 27 skipped, 6 failed** |
| Compilation | `py_compile` on the changed modules + test file | exit 0 |
| Whitespace | `git diff --check` | clean |

The six full-suite failures are the documented pre-existing ones (five
documentation-drift assertions in `test_database_setup_order`,
`test_private_message_db_security`, `test_aed_db_security`,
`test_canonical_schema_reconciliation`, and the known scheduler-recovery flake);
none touches a file changed here and none was weakened.

The focused tests assert in particular:

- a summary edit reaches the Helper Bot with the EXACT
  `InputBotInlineMessageID64` from the callback, and the self client records no
  edit;
- media results are bounded to `appearance_service.MAX_CAPTION_CHARS` with the
  marker, text results keep the full summary;
- `ChatAdminRequiredError` is logged as `kind=permission` and
  `MessageIdInvalidError` as `kind=invalid_reference`, with no
  `HISTORY_RESULT_DELIVERED`;
- no inline handle fails as `kind=no_inline_panel_handle` with zero edits, zero
  self-client edits and zero lifecycle replacements;
- the module contains no `Saved Messages`, `replace_panel`, `get_lifecycle`,
  `_RESULT_VIEWS` or old failure reasons, and constructs no Telegram client.

## 6. Original panel: edited in place

Automated tests prove the edit is addressed to the original inline message
handle. Telegram actually permits this because the Helper Bot owns the inline
result — the same API is already used for Appearance panels. **No live Telegram
edit was performed from this workspace**, so end-to-end delivery is not claimed
(see §8).

## 7. Saved Messages

The History delivery path contains **no** Saved Messages fallback, no
replacement panel, no summary sent to Saved Messages, and no instruction to
rerun from Saved Messages. The former owner-private gate and replacement code
were deleted. Unrelated features that legitimately write to Saved Messages
(Deep Save, retrieval) are untouched.

## 8. Live verification status and remaining limitations

- **Not live-verified.** This workspace has no Telegram session, helper bot
  token, Render access or Supabase credentials. No real History run was issued
  and no production log after the fix was observed. Verification requires
  pressing Run Summary on a History panel and confirming the panel edits in
  place with no `HISTORY_RESULT_DELIVERY_FAILED` line.
- A photo panel's caption cannot carry the full 2800-character summary; the
  delivered text is bounded and marked. This is a Telegram message-type limit,
  not a truncation bug, and the panel is never replaced to gain space.
- A Helper Bot that is disconnected, removed from the chat, or not the owner of
  the inline result cannot edit. The failure is logged by type and the panel
  keeps its previous content; nothing is redirected.
- If a future panel is created as an ordinary message instead of an inline
  result, no inline handle exists and delivery fails honestly; creating a second
  panel was explicitly rejected.

## 9. Git delivery

One scoped commit on top of `c18a2d5`, pushed directly to `origin/main` (no
branch, no pull request, no rebase, no force-push, no history rewrite). The
commit SHA, push result and independently re-fetched remote HEAD agreement are
recorded in the delivery message for this task. The working tree is clean.
No Render deployment was triggered; the service's auto-deploy is outside this
workspace's control and must be observed separately.
