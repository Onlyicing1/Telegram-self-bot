# IMPLEMENTATION_REPORT.md — Reply Mode: early trigger deletion + one atomic final panel edit

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. The album-as-one-item pipeline,
> metadata handling, retrieval/preview/edit/delete, Save by Link and the AI Save
> tools are unchanged and still hold. Older reports remain readable in Git
> history.

| Item | Value |
|---|---|
| Stage | Save → Deep Save → **Reply Mode**: the owner's reply is temporary input and is deleted the moment its target is known; the finished panel is now rendered in ONE edit |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `78d4db750708993ab5677ff35540f6fb6955fb6f` (`feat(save): add contract-compliant success panel actions`) |
| Related evidence | `INVESTIGATION.md` (unchanged: it concludes that the pinned library can edit an inline-result message by identifier, and that Telegram-side identifier delivery for these panels is still unproven) |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment, no Render shell |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md`, `INVESTIGATION.md` |
| Interpreter used for verification | CPython 3.12 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest` + `pytest-asyncio` |

---

## 1. What changed

Two narrowly scoped improvements to the existing Reply Mode flow; nothing else in
the Save workflow moved.

1. **The trigger reply is deleted immediately.** Previously the owner's reply was
   removed only after the whole save had finished, so a large transfer left the
   temporary input sitting in the chat for its entire duration. It is now removed
   as soon as `reply_to_msg_id` has been read — before the target lookup, before
   any download/upload, before persistence.
2. **The finished panel is one edit.** Previously the terminal text was written
   first and the action keyboard attached with a second edit. Now the reporter is
   closed without an edit of its own and the complete terminal state — details
   **and** the five actions — is written in a single
   `editInlineBotMessage`.

## 2. Trigger deletion — exact timing and safeguards

`backend/bot/handlers/save.py::_save_reply_wait_handler` now runs:

```
panel edit: "Resolving the target message…"        (existing feedback, unchanged)
read the trigger reply  (client.get_messages(chat_id, ids=msg_id))
  → capture reply_to_msg_id  (None when the message is not a reply)
delete the trigger       (_delete_trigger_message → client.delete_messages(chat_id, [msg_id]))
resolve the target       (client.get_messages(chat_id, ids=target_id))
execute_save(...)        (download → upload → persist, may take minutes)
one terminal panel edit  (details + actions)
```

* **Before any slow work** — the deletion is the very next request after the
  trigger read. A slow or failing `execute_save` cannot postpone it; a test
  asserts that at the moment the save starts, the delete has already been
  recorded.
* **Only the trigger** — the delete call is `delete_messages(chat_id, [msg_id])`
  with the handler's own `msg_id`. The target (`reply_to_msg_id`) is never in
  that list, and nothing is inferred from chat history.
* **Exactly once** — the old trailing delete block was removed, so the flow has
  one deletion site. A refused deletion is logged and swallowed; it never
  cancels the save and is not retried.
* **Missing pieces fail safe** — an unfetchable trigger yields
  `Could not find your reply message. Please try again.`, a non-reply yields
  `Your message was not a reply. …`, a vanished target yields
  `The replied message no longer exists.`; none of them saves anything, and each
  still only ever addresses the trigger id. In every case the temporary trigger
  message is removed (deleting an already-missing id is a no-op on Telegram).
* **The panel is untouched by it** — the panel is a different message, addressed
  by its own inline identifier/chat+message coordinates; deleting the trigger
  cannot delete, replace or invalidate it.
* **Same principal** — the self client (the account that sent the reply) performs
  the deletion; no new client, listener, poller or scheduler exists.

## 3. Final panel — one edit, no redundant render

`backend/helper/panel_edit.py::ExistingPanelEditor.edit(text, buttons=None)`
already accepted an optional keyboard; the handler now uses it exactly once:

```python
if saved_code and editor.path == "helper-inline":
    await reporter.close(None)              # terminal, no Telegram request
    await editor(result, success_action_rows(saved_code))   # ONE edit
else:
    await reporter.close(result)            # text-only terminal (self-client path)
```

* `reporters.close(None)` marks the reporter terminal and returns without calling
  Telegram, so a late byte callback can no longer overwrite the finished panel —
  without costing an edit.
* The single edit carries the unchanged result text (`Saved Successfully`, the
  real `Code`, `Type`, `Filename`) plus the five rows in their established order:
  `✎ Edit this item` (`panel:retrieve_item:id:<code>`), `✓ Save another item`
  (`panel:save:type:d`), `⌕ Show the saved items` (`panel:retrieve_saved`),
  `⌂ Home`, `× Close`.
* No text-only terminal render precedes it: tests assert the terminal text
  appears exactly once in the panel's edit history and that the last edit
  contains both details and five buttons.
* The `edit` action's callback is built from `result.save_code` — the code just
  issued — never from conversation context.

## 4. Unavoidable / deliberate extra edits

* **Progress edits** — real byte callbacks still drive the five-segment bar
  (unchanged). They carry no `buttons`, so MTProto leaves the existing keyboard
  alone (three explicit deltas (`buttons=None`, built rows, `buttons=[]`) were
  verified earlier against pinned Telethon; `buttons=[]` builds the wrong markup
  type and is never sent).
* **The "Resolving the target message…" line** — written before the trigger read
  so a slow resolution is visible instead of silent. It is not a duplicate of the
  result and is not a no-op.
* **Self-client fallback terminal edit** — a user account cannot attach a bot
  keyboard, so that path renders the details only; the keyboard it found is kept.
* **Refused terminal inline edit** — `ExistingPanelEditor` retries the same
  message through its chat/message id with text only, so a refused inline edit
  degrades to the details rather than losing them. This only happens on failure.

No sleeps, debounce delays, artificial animations or fake progress were added.

## 5. Preserved behaviour

* Same-panel editing and the exact inline-message identifier handling are
  untouched; Reply Mode still sends **no** progress/result message.
* `execute_save` (target resolution, download, upload, album-as-one-item,
  persistence) and the whole service layer are unchanged.
* Save by Link still creates its own single status message; its tests pass
  unchanged.
* Retrieval, preview, rename/filename/tags/move, delete safeguards, the AI Save
  tools and panel lifecycle/close behaviour are unchanged.
* Style contract: all five rows stay `PRIMARY` (Telegram has three styles; the
  project reserves green for confirmation and red for cancellation). Labels are
  registry glyph + plain ASCII text — no emoji, no variation selectors.

## 6. Changed files

| File | Change |
|---|---|
| `backend/bot/handlers/save.py` | `_delete_trigger_message`; read-reply → delete → resolve target ordering; single terminal edit (reporter closed without text on the Helper Bot path); trailing delete removed |
| `backend/helper/panel_edit.py` | `edit(text, buttons=None)` and markup-aware dedupe (from the previous change, still the path that makes the one-edit render possible) |
| `tests/test_save_reply_panel_edit.py` | early-deletion contract: 6 new tests (ordering, slow save, exact target, missing reply, missing target, refused deletion) + updated deletion/terminal-render expectations |
| `tests/test_save_success_panel.py` | one-atomic-edit assertions; the refused-terminal test now proves the details fall back to the same message |
| `AGENTS.md` | §6: trigger deletion timing + one-edit finished panel |
| `IMPLEMENTATION_REPORT.md` | this report |

## 7. Tests (executed)

| Command | Result |
|---|---|
| `pytest tests/test_save_reply_panel_edit.py -q` | **30 passed** |
| `pytest tests/test_save_success_panel.py -q` | **23 passed** |
| Save/panel batch (success panel, reply panel, live status, album/view-once, save V2 wiring/metadata/resolution, helper callback receipt, save engine) | **289 passed** |
| `pytest tests -q` (full suite) | **6683 passed, 27 skipped, 1 failed** |
| `python -m compileall -q backend tests/test_save_reply_panel_edit.py tests/test_save_success_panel.py` | OK |
| `git diff --check` | clean |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"). It is **pre-existing and unrelated**:
it reproduces identically on a clean `git archive` checkout of this change's
parent `78d4db7` (and of `476f613`), with no Save code involved.

New / updated focused coverage:

* delete happens between the trigger read and the target read (ordered trace);
* a slow save starts only after the deletion is recorded;
* `execute_save` receives exactly the reply's target id;
* the target id is never deleted; exactly one delete request exists;
* a non-reply trigger is deleted and nothing is saved;
* a missing trigger and a missing target each fail safely with the documented
  message and no transfer;
* a refused deletion leaves the save successful, once attempted;
* the terminal edit carries details + all five actions in one call, with no
  text-only terminal edit before it;
* progress edits still pass no markup (keyboard preserved) and dedupe still
  distinguishes "same text, new buttons";
* failure/refusal results still render no success actions.

## 8. Not verified against Telegram

* That Telegram accepts the single `editInlineBotMessage` carrying the new
  keyboard, and that the inline identifier is delivered at all, remain unproven
  without one real click (`[SAVE_UI] reply mode panel edit path=…`).
* The latency improvement is **application-level**: three sequential RPCs on the
  old path are now two (one terminal edit instead of a text edit plus a keyboard
  edit), and the trigger deletion no longer waits for the transfer. No claim is
  made about literal network latency.
* Whether a live client shows the panel flipping at the same moment as the
  buttons is a real-device observation.

## 9. Manual verification

1. `Menu` → `Save` → `Deep Save` → `Reply Mode`, reply to a media message.
2. Confirm the reply disappears **immediately** (before the progress bar moves),
   while the panel stays and keeps updating.
3. Confirm the panel ends with the details and the five actions in one step, with
   no intermediate button-less result.
4. Use each action: the item panel for the code just issued, the Save selection
   panel, the saved-items list, Home, and Close.
5. Repeat with a non-reply message and with a deleted target: the correct message
   appears, nothing is saved, and only the trigger is removed.

## 10. Limits left in place deliberately

* Pending input still expires after 120 s; Reply Mode's transfer timeout remains
  `None` for the listener.
* Navigating away mid-save still supersedes the save's writes (the save itself
  completes and is recorded).
* When no inline identifier is delivered, the finished panel shows the details
  but no actions, because a user account cannot attach a bot keyboard.
* Telegram's three button styles leave no compliant way to colour navigation
  apart from an action, so all rows stay `PRIMARY`.
